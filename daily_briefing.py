"""
daily_briefing.py — proactive morning brief.

Runs once a day at JARVIS_BRIEFING_HOUR:JARVIS_BRIEFING_MINUTE (local time,
default 08:00), builds the agenda text via productivity_service (pure
formatting, no LLM call — see its docstring for why: this fires
unattended, so it shouldn't depend on a Groq call succeeding or burn quota
on a summary a template can produce just as well), synthesizes it to audio
with the same edge-tts voice as live commands, and pushes it over the
existing SSE stream (event_stream.py) for the HUD to autoplay.

This complements, not replaces, the on-demand `get_daily_agenda` tool —
say "what's my day look like" any time and the LLM calls that tool
directly; this module only handles the unprompted once-a-day push.
"""

import asyncio
import logging
import os
import threading
import time
from datetime import datetime, timedelta

import event_stream
import productivity_service
from tts import generate_tts_base64

logger = logging.getLogger("jarvis.daily_briefing")

ENABLED = os.environ.get("JARVIS_DAILY_BRIEFING", "true").lower() == "true"
BRIEFING_HOUR = int(os.environ.get("JARVIS_BRIEFING_HOUR", "8"))
BRIEFING_MINUTE = int(os.environ.get("JARVIS_BRIEFING_MINUTE", "0"))

_CHECK_INTERVAL_SECONDS = 60


def _next_fire_time(after: datetime) -> datetime:
    candidate = after.replace(hour=BRIEFING_HOUR, minute=BRIEFING_MINUTE, second=0, microsecond=0)
    if candidate <= after:
        candidate += timedelta(days=1)
    return candidate


def _run_briefing_once():
    text = productivity_service.get_daily_agenda_text()
    audio = asyncio.run(generate_tts_base64(text))
    event_stream.push_event({"type": "daily_briefing", "text": text, "audio": audio})
    logger.info("Daily briefing pushed.")


def _scheduler_loop():
    next_fire = _next_fire_time(datetime.now())
    logger.info(f"Daily briefing scheduled for {next_fire.strftime('%Y-%m-%d %H:%M')}.")
    while True:
        time.sleep(_CHECK_INTERVAL_SECONDS)
        now = datetime.now()
        if now >= next_fire:
            try:
                _run_briefing_once()
            except Exception as e:
                logger.error(f"Daily briefing failed: {e}")
            next_fire = _next_fire_time(now)
            logger.info(f"Next daily briefing scheduled for {next_fire.strftime('%Y-%m-%d %H:%M')}.")


def start_scheduler():
    """Call once at app startup. No-op if disabled or nothing is connected
    yet, so a fresh install without Google/Outlook configured doesn't spin
    up a thread that will only ever produce 'nothing connected' pushes.

    MULTI-USER BUILD: this module still assumes one global agenda for one
    account — get_daily_agenda_text() now requires a user_id, and there's no
    per-user schedule/SSE-subscriber routing here yet. Rather than either
    crash or (worse) silently broadcast one user's calendar to every
    connected browser, the scheduler is disabled outright for this build.
    A real per-user version (each user's own briefing time, delivered only
    to their own session) is a follow-up feature, not a phase-1 requirement.
    """
    logger.info("Daily briefing scheduler disabled in the multi-user build (not yet per-user — see docstring).")
    return
