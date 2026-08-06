# J.A.R.V.I.S — Cloud multi-user fork — Claude Code notes

Multi-user cloud voice assistant, deployed at **https://jarvis-cloud-oifj.onrender.com**
(Render free tier). Anyone with a Google account added as a Test user can sign
in from a phone or computer browser and get their own calendar/inbox/voice
assistant. Two pillars remain from the original single-user app; the third
(desktop automation) is intentionally out of scope here — there's no user
device for a cloud server to control:

1. **Schedule/email management is the central use case** — calendar + inbox.
2. Conversational voice assistant (Groq LLM + edge-tts, browser HUD in
   [templates/index.html](templates/index.html)). Speech recognition
   (`recognition.lang`) is Hebrew (`he-IL`) — the actual user speaks Hebrew —
   but JARVIS's own replies stay English (`SYSTEM_PROMPT` says so explicitly;
   tried full Hebrew TTS once, the two available voices — he-IL-AvriNeural/
   HilaNeural — read as unprofessional, reverted). Wake/stop/yes-no phrase
   matching accepts both languages since he-IL recognition isn't guaranteed
   to transcribe English loanwords.
3. Flexible general-purpose agent — but trimmed hard from the original: no
   Spotify (removed — it was one server-wide shared account, not per-user
   OAuth), no generic web_search/play_media (removed — both called Python's
   `webbrowser.open()` **server-side**, which opens nothing on a remote
   user's device; that only worked in the original single-tenant desktop
   app because the server and the user were the same machine), no desktop
   automation (`JARVIS_DESKTOP_TOOLS=false` on Render — `open_application`/
   `close_application`/`computer_use` are omitted from the tool list sent to
   Groq entirely when this is false, not just refused at call time — saves
   tokens against the free-tier rate limit too). What's left: calculate,
   get_weather, write_and_test_code + file tools (now per-user sandboxed,
   see below), the schedule/email/reminder tools, `find_nearby_places`
   (confirm-to-open Google Maps search, see below), `get_market_summary`
   (free market-index lookup, no AI involved, see below), and
   `get_current_info` (general news/current-events lookup via
   [tavily_service.py](tavily_service.py), see below) — the last three are
   the only tools that touch anything outside the app's own data, and all
   are deliberately scoped narrow.

## ⚠️ Do not confuse this with the other two JARVIS folders

- `C:\Users\User\Desktop\free 400 שקל` — the **single-user** desktop app the
  user runs day to day on their own PC. Has its own separate CLAUDE.md.
- `C:\Users\User\Desktop\jarvis_final` — where single-user development happens.
- **This folder** (`האפליקציה הסופית`) is a **separate fork**, created
  specifically to become a multi-tenant cloud product without touching or
  risking the working single-user app. Code does not sync between them —
  changes made here stay here unless explicitly asked to port them back.

## Deployment

- Render Blueprint: [render.yaml](render.yaml). `gunicorn app:app --workers 1
  --threads 16`. **Exactly one worker is load-bearing** — `daily_briefing.py`'s
  scheduler thread starts once at module import time (not gated behind
  `if __name__ == "__main__"`, which gunicorn never executes — this was a
  real bug: the scheduler never ran in production until fixed). More than
  one worker would start multiple copies and double/triple-send reminders.
  Threads=16 because each open browser tab holds one `/api/stream` (SSE)
  connection open indefinitely; 2 threads meant two simultaneous tabs
  exhausted the pool and hung every other request including sign-in.
- **Free tier, deliberately** (user chose $0 over persistence): no disk, so
  `users.db` — accounts, Google tokens, **and reminders** — is wiped on every
  restart/spin-down (~15 min idle). A reminder set for later can silently
  vanish if the server recycles before it fires. Real persistence needs a
  durable store (Render's free Postgres is the not-yet-taken next step).
- Two separate Google OAuth clients exist in the same Cloud project: a
  "Desktop app" one (used by `free 400 שקל`, loopback-only) and a "Web
  application" one (`GOOGLE_CLIENT_ID`/`SECRET` here, registered redirect URI
  must exactly match the live Render URL — it changed once already when
  Render appended a random suffix to the service name).
- Google OAuth consent screen is in **Testing** status — only emails added
  under Audience → Test users can sign in at all (`console.cloud.google.com/auth/audience`).
  This is also the access-control mechanism keeping the free-tier Groq quota
  from strangers, not just a dev inconvenience.

## Multi-user architecture (the part that's different from the single-user app)

- [users.py](users.py) — SQLite (`users.db`), one row per Google account
  (`id` = Google's `sub`), replacing the single-tenant `.google_cache/token.json`.
  Also holds the `reminders` table.
- [guardrails.py](guardrails.py) — `user_workspace_dir(user_id)` confines
  `write_and_test_code`/file tools to a per-user subtree
  (`WORKSPACE_DIR/users/<id>/`) instead of one shared folder — **this was a
  real cross-user bug**: before the fix, any signed-in user could read/
  overwrite/delete any other user's files. `resolve_safe_path(path, user_id=...)`
  is now the required form for any multi-user caller.
- [event_stream.py](event_stream.py) — SSE pub/sub, **keyed by user_id**.
  `push_event(event, user_id)` requires the id (no silent-broadcast default)
  — the original single-tenant version broadcast every event to every
  connected tab, which in the multi-user build meant one user's proposed
  calendar event/drafted email was pushed to every *other* signed-in user's
  browser too. Fixed; `/api/stream` now subscribes per-session.
- **Confirm-to-act ownership**: `app._confirm_owner_denial(token)` verifies
  the requesting session's `user_id` against the token's stored
  `meta["user_id"]` before `/api/confirm/<token>` or its `/edit` variant do
  anything — added after the event_stream fix above, since a leaked token
  without this check could still be acted on by the wrong person.
  `guardrails.request_confirmation(..., meta={...})` / `update_pending_meta()`
  carry that ownership (and, for composed emails, attachments) through edits.
- **Reminders & weekly summary** ([daily_briefing.py](daily_briefing.py)) —
  delivered by **email** (`google_service.send_email`, through the user's own
  Gmail connection), not SSE — SSE only reaches a tab that's open right now,
  which defeats the point of a reminder. One background thread, checked once
  a minute: due reminders (`users.get_due_reminders`) get emailed and marked
  delivered; weekly summary (`JARVIS_WEEKLY_SUMMARY`, default Sunday 08:00
  local) emails every currently-connected user
  (`users.list_connected_user_ids`) their week ahead.
- **Compose Email modal** (`/api/compose/draft` + `/api/compose/send` in
  app.py, UI in index.html) — a proper form (To/Reason/Files) instead of a
  canned chat command. Draft asks Groq for a `SUBJECT:`/`BODY:` formatted
  response (not JSON — more forgiving of an LLM's punctuation than strict
  JSON parsing would be). Attachments travel as base64 inside JSON (not
  multipart/form-data, for consistency with the rest of the API), capped at
  `MAX_ATTACHMENTS=3` / `MAX_ATTACHMENT_BYTES=4MB` each — real limits, not
  decorative, enforced both client-side (immediate feedback) and server-side.
- Groq's `_time_context()` (app.py) injects the real current local date/time
  + UTC offset into every request — without it the model either guessed
  dates or stamped the user's spoken local time with a `Z` (UTC) suffix
  unchanged, silently shifting every calendar event/reminder by the local
  offset. `LOCAL_TZ` (`JARVIS_TIMEZONE` env var, default `Asia/Jerusalem`)
  is used consistently for this, for `_format_time()`, and for the weekly
  scheduler's fire-time calculation.

## Why get_current_info exists (Groq has no live internet access)

Groq's models (everything else in this app runs on Groq — `MODEL_NAME` in
app.py) are static, frozen at their training cutoff, with zero live internet
access — asked "did the stock market drop this week", they can only guess
or admit they don't know. Unlike ChatGPT's web app, JARVIS never had a real
web-browsing tool wired in — the old `web_search` tool (removed earlier)
only ever called `webbrowser.open()`, it never fed search results back to
the model either. General market-mood questions specifically are answered
for free with zero search at all — see `get_market_summary` below.

[tavily_service.py](tavily_service.py) fixes the general case: Tavily's
free tier (no credit card, 1,000 requests/month) is purpose-built for
LLM search — feed it a question, get back a synthesized answer plus the
sources it drew from. Wired to exactly one tool, `get_current_info`, used
only when the question needs current information the frozen Groq model
can't know — SYSTEM_PROMPT is explicit that this isn't a general second
LLM backend or an excuse to search reflexively (same "narrow and
deliberately so" philosophy as `find_nearby_places`). Needs its own
`TAVILY_API_KEY` env var (get one at https://tavily.com — sign up, key is
generated automatically); degrades to a spoken "not set up" message
rather than failing if unset. (An earlier version of this used Gemini
instead — its free tier turned out to require Google Cloud billing setup
even to use, which defeated the point; replaced outright rather than kept
as a fallback.)

## Why get_market_summary exists (free, zero AI, for market questions)

[stocks_service.py](stocks_service.py)'s `get_market_summary()` answers
"how's the market doing" / "did stocks drop this week" by fetching the
major US indices (S&P 500, Dow, Nasdaq) from the same zero-key Yahoo
Finance endpoint the `📈 מניות` HUD button uses — no LLM call, no API key,
can't be blocked by any billing gate. Listed ahead of `get_current_info`
in both the TOOLS schema and each tool's own description, so the model
reaches for the free one first for general market-mood questions;
`get_current_info` stays for genuinely non-market current-events
questions.

## Known gotchas

- **Groq free-tier rate limit is tight** — a brand-new free account
  exhausted its quota after ~6 demo commands. Every round trip resends the
  *entire* history + system prompt + every tool schema, and
  `MAX_TOOL_ROUNDS` chains multiple calls per single user command. Already
  mitigated: `MODEL_NAME="openai/gpt-oss-20b"` (not 120b — much more
  generous free budget), `MAX_TOOL_ROUNDS=2`, `MAX_HISTORY_MESSAGES=12`,
  and `ACTIVE_TOOLS` trims desktop-only tool schemas entirely rather than
  just refusing them at call time. If it's still hit constantly, the actual
  fix is Groq's paid (cheap, pay-as-you-go) tier — creating new free
  accounts is not a real solution, confirmed by direct experience.
- **`MAX_TOKENS` must stay generous (1024, not 200)** — gpt-oss models spend
  tokens on a hidden "analysis" channel before the tool-call JSON, and Groq
  counts that against `max_tokens`. Too low → truncated JSON → 400
  `tool_use_failed`. `_complete_with_retry()` retries once on that specific
  error, but don't shrink `MAX_TOKENS` back down.
- **Mobile performance**: this HUD has several full-viewport
  `backdrop-filter: blur()` layers (including the sign-in gate, visible
  immediately on load) plus multiple simultaneous glow/rotation animations
  — reported as a visible freeze on first paint on phone that desktop didn't
  have. `@media (hover: none) and (pointer: coarse)` drops backdrop-filter
  and the purely decorative animations on touch devices; desktop keeps the
  full effect (mouse/trackpad has hover+fine pointer regardless of window
  width).
- **Mixed Hebrew/English text renders out of order** without `dir="auto"` —
  the page has no `dir="rtl"`, so a Hebrew sentence with an embedded English
  quoted term (`say "WAKE UP" or "good morning"`) visibly scrambles word
  order. Every element that can hold this kind of mixed text needs
  `dir="auto"` (mic-gate title, hologram display, status line, error text,
  each dynamically-created log entry) — the browser then picks each
  element's own direction from its first strong character.
- **No calendar-event or email delete/edit-after-send tools exist** — only
  create, and only *before* approval (the Compose/Inbox-reply EDIT button
  rewrites a still-*pending* proposal, not something already sent). Test
  events created during verification get left on the real calendar to
  remove manually.
- `spotify_login.py`, `SPOTIFY_*` env vars, and the `spotipy` dependency are
  gone from this fork entirely (removed, not just disabled) — don't
  reintroduce references to them.

## Setup / running locally (rare — this fork is meant to run on Render)

```
pip install -r requirements.txt
python google_login.py      # Desktop-type OAuth client only; Render uses the Web-type client + env vars instead
python app.py
```
See [README.md](README.md) for the full Google Cloud Console + Render
walkthrough. The user is non-technical with terminals/dashboards and needs
very explicit, field-by-field, screenshot-driven guidance through any of
this — established pattern, not a one-off preference.
