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
import time
from datetime import datetime, timedelta, timezone
from email.utils import parseaddr
from zoneinfo import ZoneInfo

import google_service
import microsoft_service
import users
from guardrails import request_confirmation, get_pending_meta, update_pending, update_pending_meta

logger = logging.getLogger("jarvis.productivity")

# Same zone as app.py's LOCAL_TZ (same env var, same default) — kept as a
# separate constant rather than imported from app.py to avoid a circular
# import (app.py imports this module). Used so "today"/spoken times reflect
# the user's actual local day, not the server's (Render's containers run on
# UTC, so a naive datetime.now() near midnight could report the wrong date).
LOCAL_TZ = ZoneInfo(os.environ.get("JARVIS_TIMEZONE", "Asia/Jerusalem"))


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
        return dt.strftime("%a %H:%M")
    except ValueError:
        return iso_str


def get_calendar_events_text(user_id: str, days_ahead: float = 1, max_results: int = 15) -> str:
    events = _collect_events(user_id, days_ahead, max_results)
    if events is None:
        return "No calendar is connected, sir — please sign in first."
    if not events:
        return "Nothing on the calendar in that window, sir."

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
        {
            "id": e.get("id", ""), "summary": e.get("summary", "(no title)"),
            "time_label": _format_time(e.get("start")), "source": e.get("source", ""),
        }
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
        return "No mailbox is connected, sir — please sign in first."
    if not emails:
        return "Inbox is clear, sir." if unread_only else "No recent messages, sir."

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
    break just because a Groq key is flaky.

    Deliberately just names senders (grouped, with a count if there's more
    than one from the same person) rather than reading every subject line —
    the full subjects are already visible in the inbox card list, and having
    this read out loud too made checking the inbox by voice feel like being
    read a wall of text instead of a quick spoken headline."""
    if not emails:
        return "Your inbox is clear, sir."
    counts = {}
    order = []
    for e in emails:
        name = e["sender_name"]
        if name not in counts:
            counts[name] = 0
            order.append(name)
        counts[name] += 1
    senders = ", ".join(f"{counts[name]} from {name}" if counts[name] > 1 else f"one from {name}" for name in order)
    count = len(emails)
    return f"You have {count} unread email{'s' if count != 1 else ''}, sir — {senders}."


def get_daily_agenda_text(user_id: str) -> str:
    """Pure string formatting, no LLM call — used both as a tool result
    (so voice queries like 'what's my day look like' work any time) and by
    daily_briefing.py's scheduler (so the proactive morning brief doesn't
    spend a Groq API call it doesn't need)."""
    today_label = datetime.now(LOCAL_TZ).strftime("%A, %B %d")

    if not any_calendar_configured() and not any_mail_configured():
        return (
            f"Good morning, sir. Today is {today_label}. "
            "No calendar or mailbox is connected yet — see the README to set one up."
        )

    events = _collect_events(user_id, days_ahead=1, max_results=15) or []
    emails = _collect_emails(user_id, unread_only=True, max_results=10) or []

    parts = [f"Good morning, sir. Today is {today_label}."]

    if events:
        parts.append(f"You have {len(events)} item(s) on the calendar:")
        for e in events:
            where = f" at {e['location']}" if e.get("location") else ""
            parts.append(f"  {_format_time(e['start'])} — {e['summary']}{where}")
    elif any_calendar_configured():
        parts.append("Nothing on the calendar today.")

    if emails:
        # Names only (grouped/counted), not every subject line — see
        # get_inbox_summary_spoken's docstring for why.
        counts = {}
        order = []
        for e in emails:
            name = parseaddr(e.get("sender", ""))[0] or e.get("sender", "unknown")
            if name not in counts:
                counts[name] = 0
                order.append(name)
            counts[name] += 1
        senders = ", ".join(f"{counts[n]} from {n}" if counts[n] > 1 else f"one from {n}" for n in order)
        parts.append(f"There are {len(emails)} unread email(s): {senders}.")
    elif any_mail_configured():
        parts.append("Inbox is clear.")

    return "\n".join(parts)


def get_weekly_summary_text(user_id: str) -> str:
    """Same zero-LLM philosophy as get_daily_agenda_text — this is what
    daily_briefing.py's scheduler emails out once a week, unattended, so it
    can't depend on a Groq call succeeding."""
    if not any_calendar_configured():
        return "No calendar is connected, sir — see the README to set one up."

    events = _collect_events(user_id, days_ahead=7, max_results=30) or []
    if not events:
        return "Nothing on your calendar for the week ahead, sir."

    parts = [f"Your week ahead — {len(events)} item(s):"]
    for e in events:
        where = f" at {e['location']}" if e.get("location") else ""
        parts.append(f"  {_format_time(e['start'])} — {e['summary']}{where}")
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
        return {"status": "refused", "message": "No calendar is connected, sir — please sign in first."}

    if target == "google":
        impl = google_service.create_calendar_event
        cb_args = (user_id, summary, start_iso, end_iso, description, location)
    else:
        impl = microsoft_service.create_calendar_event
        cb_args = (summary, start_iso, end_iso, description, location)

    description_text = f"Create calendar event '{summary}' ({start_iso} - {end_iso}) on {target.title()}"
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


def request_delete_calendar_event(user_id: str, event_id: str, summary: str = "") -> dict:
    """Cancel-gated the same way create is — nothing is ever removed from
    the user's real calendar without an explicit approval on the HUD.
    Google-only: microsoft_service.list_calendar_events doesn't return
    event ids at all (and isn't configured in this build anyway — see this
    module's docstring), so there's nothing to target for a Microsoft
    event yet."""
    if not event_id:
        return {"status": "refused", "message": "No event was specified, sir."}
    if not google_service.CONFIGURED:
        return {"status": "refused", "message": "No calendar is connected, sir — please sign in first."}

    description_text = f"Cancel calendar event '{summary or event_id}'"
    token = request_confirmation(
        description_text, google_service.delete_calendar_event, user_id, event_id,
        meta={"kind": "calendar_event_delete", "user_id": user_id},
    )
    return {
        "status": "confirmation_required", "token": token, "message": description_text,
        "kind": "calendar_event_delete",
        "details": {"summary": summary or event_id},
    }


def edit_pending_calendar_event(token: str, summary: str, start_iso: str, end_iso: str,
                                 description: str = "", location: str = "") -> dict:
    """Rewrites a still-pending calendar-event proposal in place, so the
    next approve/deny acts on the corrected fields instead of the original
    ones. Refuses if the token expired or was never a calendar-event
    proposal — same failure mode as approving/denying a stale token."""
    meta = get_pending_meta(token)
    if not meta or meta.get("kind") != "calendar_event":
        return {"status": "error", "message": "That confirmation has expired or doesn't exist, sir."}

    target = meta["provider"]
    if target == "google":
        cb_args = (meta["user_id"], summary, start_iso, end_iso, description, location)
    else:
        cb_args = (summary, start_iso, end_iso, description, location)

    description_text = f"Create calendar event '{summary}' ({start_iso} - {end_iso}) on {target.title()}"
    if not update_pending(token, cb_args, description_text):
        return {"status": "error", "message": "That confirmation has expired or doesn't exist, sir."}

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


def request_send_email(user_id: str, to: str, subject: str, body: str, provider: str = "auto",
                        attachments: list = None) -> dict:
    target = _pick_mail_provider(provider)
    if target == "none":
        return {"status": "refused", "message": "No mailbox is connected, sir — please sign in first."}

    if target == "google":
        impl = google_service.send_email
        cb_args = (user_id, to, subject, body, attachments)
    else:
        # microsoft_service.send_email doesn't support attachments (and isn't
        # configured in this build anyway — see the module docstring).
        impl = microsoft_service.send_email
        cb_args = (to, subject, body)

    description_text = f"Send email to {to}: '{subject}' via {target.title()}"
    token = request_confirmation(
        description_text, impl, *cb_args,
        meta={"kind": "email", "provider": target, "user_id": user_id, "attachments": attachments},
    )
    return {
        "status": "confirmation_required", "token": token, "message": description_text,
        "kind": "email",
        "details": {
            "to": to, "subject": subject, "body": body, "provider": target.title(),
            "attachments": [a["filename"] for a in (attachments or [])],
        },
    }


def edit_pending_email(token: str, to: str, subject: str, body: str, attachments: list = None) -> dict:
    """Same idea as edit_pending_calendar_event() but for a pending send_email
    proposal — swaps the stored args so approval sends the corrected message.
    attachments, when omitted, keeps whatever was already attached (read
    back from meta) — the HUD's edit form doesn't re-render a file picker,
    so a typo fix to the subject shouldn't silently drop the attachment."""
    meta = get_pending_meta(token)
    if not meta or meta.get("kind") != "email":
        return {"status": "error", "message": "That confirmation has expired or doesn't exist, sir."}

    if attachments is None:
        attachments = meta.get("attachments")

    target = meta["provider"]
    if target == "google":
        cb_args = (meta["user_id"], to, subject, body, attachments)
    else:
        cb_args = (to, subject, body)

    description_text = f"Send email to {to}: '{subject}' via {target.title()}"
    if not update_pending(token, cb_args, description_text):
        return {"status": "error", "message": "That confirmation has expired or doesn't exist, sir."}
    update_pending_meta(token, attachments=attachments)

    return {
        "status": "ok", "token": token, "message": description_text,
        "kind": "email",
        "details": {
            "to": to, "subject": subject, "body": body, "provider": target.title(),
            "attachments": [a["filename"] for a in (attachments or [])],
        },
    }


def request_set_reminder(user_id: str, text: str, remind_at_iso: str) -> dict:
    """Reminders don't go through the confirm-to-act gate that calendar
    events/emails do — unlike those, nothing external happens and nobody
    else is affected; it's a note-to-self stored in our own database, not
    an action on the user's real calendar/mailbox. Delivered later by
    daily_briefing.py's scheduler emailing the user (see that module for
    why: SSE only reaches a tab that's currently open, and a reminder is
    useless if it only "arrives" while you're already staring at the
    screen it would have popped up on)."""
    try:
        dt = datetime.fromisoformat(remind_at_iso)
    except (ValueError, TypeError):
        return {"status": "error", "message": "I couldn't understand that time, sir."}
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=LOCAL_TZ)

    remind_at_epoch = dt.timestamp()
    if remind_at_epoch <= time.time():
        return {"status": "error", "message": "That time is already in the past, sir."}

    users.add_reminder(user_id, text, remind_at_epoch)
    when_label = dt.astimezone(LOCAL_TZ).strftime("%A, %B %d at %H:%M")
    return {"status": "ok", "message": f"I'll remind you to {text} on {when_label}, sir."}
