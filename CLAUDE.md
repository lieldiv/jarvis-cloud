# J.A.R.V.I.S — Claude Code notes

Voice personal assistant. Three pillars, in priority order:
1. **Schedule/email management is the central use case** — calendar + inbox, not a side feature.
2. Conversational voice assistant (Groq LLM + edge-tts, browser HUD in [templates/index.html](templates/index.html)).
3. Flexible general-purpose agent (open/close apps, web search, Spotify, Paint drawing, calculator, computer-use vision, code writing, file tools).

## ⚠️ There are TWO copies of this project on disk

- `C:\Users\User\Desktop\jarvis_final` — where development happens.
- `C:\Users\User\Desktop\free 400 שקל` — **the one the user actually runs day to day.**

They are not linked (no symlink/junction — verified). Code files get manually
synced/copied between them; **`.env`, `.google_cache/`, `.certs/`, `.ms_cache.bin`,
`.spotify_cache` do NOT sync automatically** (they're gitignored secrets/tokens).
If calendar/email tools suddenly report "not connected" or the assistant
hallucinates generic wrong instructions instead of using real tools, the first
thing to check is **which folder the live process is actually running from**
(`Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Select CommandLine`)
and whether that folder's `.env`/`.google_cache` are up to date. When you change
code, copy it to both folders (or ask the user which one to touch).

## Architecture

- [app.py](app.py) — Flask server, Groq tool-calling loop (`run_llm`), routes.
- [tts.py](tts.py) — shared edge-tts helper (used by app.py and daily_briefing.py).
- [cert_bootstrap.py](cert_bootstrap.py) — **must stay the first import** in any
  module that makes HTTPS calls. This machine's antivirus/network does TLS
  inspection with a root CA that Windows trusts but Python's `certifi` doesn't,
  so uncovered HTTPS calls fail with `CERTIFICATE_VERIFY_FAILED`. This patches
  `certifi.where()` + sets `SSL_CERT_FILE`/`REQUESTS_CA_BUNDLE`/`CURL_CA_BUNDLE`
  for this process only, pointing at `.certs/combined_ca_bundle.pem`
  (certifi's CAs + Windows' trusted roots, exported once via PowerShell — see
  README for the regeneration snippet if it ever goes stale).
- [google_service.py](google_service.py) / [microsoft_service.py](microsoft_service.py) —
  Google Calendar+Gmail and Outlook/365 Graph API clients. Each has a
  `CONFIGURED` flag (env vars present) and degrades to `None`/spoken-string
  instead of raising when unconfigured or libs aren't installed.
- [productivity_service.py](productivity_service.py) — merges both providers.
  Reads go straight through. **Writes (`create_calendar_event`, `send_email`)
  never execute directly** — they call `guardrails.request_confirmation()` and
  return a token; nothing happens until the user approves via the HUD modal or
  says "yes". This is the "read-only + suggestions" autonomy model — don't
  make writes fire immediately even if it seems convenient.
- [daily_briefing.py](daily_briefing.py) — background thread, fires once/day
  (default 08:00, `JARVIS_BRIEFING_HOUR`/`_MINUTE`), builds the agenda with
  **zero LLM calls** (pure string formatting) so a bad/expired Groq key can't
  take it down, pushes it over SSE.
- [event_stream.py](event_stream.py) — SSE pub/sub (`/api/stream`). The HUD's
  `EventSource` handler in index.html renders `confirmation_required` as a
  modal (`.confirm-overlay`/`.confirm-modal`) and `daily_briefing` as
  autoplaying audio + hologram text.
- [guardrails.py](guardrails.py) — sandboxing (`resolve_safe_path`), no-admin
  check, and the confirm-to-act registry (`request_confirmation`/
  `resolve_confirmation`). Every destructive/write action in the codebase
  (file delete, process kill, calendar write, email send) goes through this.
- [google_login.py](google_login.py) / [microsoft_login.py](microsoft_login.py) /
  [spotify_login.py](spotify_login.py) — one-time interactive OAuth scripts,
  run manually before first use so the browser consent doesn't block a live
  voice command.

## Known gotchas (hit these already, don't re-debug from scratch)

- **`GROQ_API_KEY` is not stored anywhere persistent** (not in User/Machine env
  vars, not originally in `.env`) — the user sets it manually per-session in
  whatever terminal launches `app.py`. Don't assume it's recoverable; ask the
  user rather than searching shell history (searching for secrets in history
  files is out of scope without explicit permission — hit this exact denial
  once already).
- **`MAX_TOKENS` must stay generous (1024, not 200)** — gpt-oss models on Groq
  spend tokens on a hidden "analysis" channel before the tool-call JSON, and
  Groq counts that against `max_tokens`. Too low → truncated JSON → 400
  `tool_use_failed`. `_complete_with_retry()` in app.py retries once on that
  specific error, but don't shrink `MAX_TOKENS` back down.
- **Google OAuth test mode**: the Cloud project's OAuth consent screen is in
  "Testing" status, so only accounts added under Audience → Test users can
  log in (`lieldiv@gmail.com` is added). Adding more Google accounts means
  adding them there first, or publishing the app.
- **SSL certificate errors are a recurring theme on this machine** — pip
  installs, OAuth token exchanges, and API calls have all hit
  `CERTIFICATE_VERIFY_FAILED` independently (different HTTP libraries don't
  all respect the same env vars — `requests` honors `REQUESTS_CA_BUNDLE`,
  `httplib2` doesn't). `cert_bootstrap.py` covers everything imported through
  this project's own modules; a bare `pip install` in a fresh terminal will
  still need `$env:PIP_CERT = "<path to .certs\combined_ca_bundle.pem>"` set
  manually first.
- Don't overwrite the global `certifi` package in site-packages to fix SSL
  issues — that was explicitly denied once (affects every Python program on
  the machine, not just JARVIS). The scoped `cert_bootstrap.py` approach is
  the sanctioned fix.
- No calendar-event or email **delete/edit** tools exist yet — only create.
  Test events created during verification get left on the real calendar for
  the user to remove manually.

## Setup / running

See [README.md](README.md) for full walkthrough (Google Cloud Console + Azure
app registration steps, scopes, `.env` variables). Quick version:
```
pip install -r requirements.txt
python google_login.py      # and/or microsoft_login.py
python app.py
```
