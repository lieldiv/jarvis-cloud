# Packaging J.A.R.V.I.S as a Windows executable

This turns the app from "open a terminal and run `python app.py`" into a
double-clickable `.exe`. It was built and smoke-tested during this hardening
pass (see "What was actually verified" below) — not just written and
assumed to work.

## Why PyInstaller

The app is a plain CPython + Flask process with a handful of native-ish
dependencies (pyautogui/pyscreeze/Pillow for screen automation, edge-tts for
async network TTS, the Google/MSAL OAuth stacks). PyInstaller is the
standard, most battle-tested option for exactly this shape of app on
Windows — no license cost, mature Windows support, well-documented hook
system for exactly the kind of "package ships weird data files/does dynamic
imports" problems this project's dependencies actually have. Alternatives
considered and rejected:
- **cx_Freeze / Nuitka** — Nuitka's compiled-to-C approach is appealing for
  speed but its handling of the exact dependency mix here (aiohttp async
  internals, googleapiclient's dynamic discovery-document loading) is far
  less proven than PyInstaller's; not worth the risk for a first packaging
  pass.
- **A web install (Electron/Tauri wrapper)** — this app *is* the server;
  wrapping it in a desktop shell buys native window chrome but doesn't
  solve any of the actual hard problems (bundling edge-tts, pyautogui,
  OAuth, cert_bootstrap), and adds a whole second toolchain (Node) to a
  project that has none today.

## What's in `jarvis.spec`

Builds four onefile executables:
- **JARVIS.exe** — the actual app.
- **jarvis_google_login.exe**, **jarvis_microsoft_login.exe**,
  **jarvis_spotify_login.exe** — the one-time OAuth setup helpers
  (`google_login.py` / `microsoft_login.py` / `spotify_login.py`), packaged
  too so a client machine never needs `python` on PATH for anything.

Key decisions baked into the spec (each one commented inline there too):
- **`console=True`, not windowed.** This app logs meaningfully (Flask
  request log, tool-call traces, the friendly startup-error messages
  app.py/guardrails.py raise via `SystemExit`) — a visible console is far
  easier for a first-time user (or the developer, remotely) to diagnose
  "it doesn't work" from than a silently-closing window.
- **`onefile=True`.** Simplest to hand a client a single file. Tradeoff:
  slower cold start (unpacks to a temp dir every launch) and a large
  (~100MB) binary since Groq/Google/MSAL/pyautogui/Pillow/aiohttp each
  bring their own dependency trees. Acceptable for a desktop assistant
  that's launched once and left running.
- **`templates/` is bundled as PyInstaller `datas`.** Flask's
  `render_template` resolves its template folder relative to the app
  module's own location, which — for a frozen onefile build — is the
  extraction directory, same relative layout as unfrozen. No code change
  needed beyond the spec's `datas` entry.
- **`.certs/` is deliberately NOT bundled.** It's a workaround for *this
  specific development machine's* antivirus/proxy doing TLS inspection
  with a root CA that Windows trusts but Python's certifi doesn't (see
  `cert_bootstrap.py`'s docstring and the README's SSL section). Bundling
  it would ship a wrong-for-everyone-else trust anchor. If the client's
  machine turns out to need this too, generate `.certs/combined_ca_bundle.pem`
  *on that machine* (PowerShell snippet in README) and drop it next to the
  installed `.exe` — `cert_bootstrap.py` already checks for it there and
  degrades gracefully (falls back to plain certifi) if it's absent.
- **googleapiclient's discovery-document JSON and edge_tts/certifi package
  data are explicitly collected** (`collect_data_files(...)` in the spec).
  These are runtime-loaded non-.py files PyInstaller's static import scan
  can't see on its own; without this, `build("calendar", "v3", ...)` /
  `build("gmail", "v1", ...)` fail at runtime with a discovery-document
  error even though every `import` line succeeded at build time.

## Code changes made specifically to support freezing

A frozen executable's `__file__`/cwd behave differently from a normal
`python app.py` run, and this app has several places that quietly assumed
"cwd == the folder this app lives in" (true by luck for a dev running from
the project folder, not guaranteed for a packaged exe or a shortcut with a
different "Start in" value):

- **`app.py`** now resolves `_APP_DIR` explicitly at the very top —
  `os.path.dirname(sys.executable)` when `sys.frozen` is set (PyInstaller),
  else the source file's own folder — and calls `os.chdir(_APP_DIR)` before
  any other local import runs. Every relative path used elsewhere
  (`.env`, `jarvis.log`, `.google_cache/`, `.ms_cache.bin`,
  `.spotify_cache`) now consistently resolves next to the actual installed
  app, launched however.
- **`cert_bootstrap.py`** does the same frozen-vs-source directory check
  for its own `.certs/combined_ca_bundle.pem` lookup (it computes this
  independently of app.py's cwd, since it uses its own `__file__`).
- **`load_dotenv()`** is now called with an explicit path
  (`os.path.join(_APP_DIR, ".env")`) instead of relying on python-dotenv's
  own cwd/caller-frame search heuristics, which behave inconsistently
  inside a frozen executable.
- **Startup failures now use `SystemExit(message)` instead of
  `raise RuntimeError(message)`** (missing `GROQ_API_KEY`, admin/elevated
  refusal, workspace-path refusal) — Python prints just the `SystemExit`
  message to stderr with no traceback, which reads like an intentional,
  informative refusal instead of a crash. Verified directly (see below).
- **`vision_action.py`** now guards its `pyautogui`/`Pillow` imports the
  same way `app.py` already guarded `pyautogui`/`pywhatkit`/`spotipy` —
  previously an import failure there (unlikely on Windows, but a real risk
  if a frozen build ever ships without those wheels resolving cleanly)
  would have taken down the whole process at startup, since `app.py`
  imports `vision_action` unconditionally. Now it disables just
  `computer_use` with a clear spoken message instead.

None of this changes behavior when run unfrozen (`python app.py` from the
project folder) — `_APP_DIR` resolves to the same folder cwd already was.

## How to build

```powershell
cd "C:\path\to\jarvis\project"      # the folder containing app.py
pip install pyinstaller
pyinstaller packaging\jarvis.spec --distpath dist --workpath build --clean
```

Output: `dist\JARVIS.exe`, `dist\jarvis_google_login.exe`,
`dist\jarvis_microsoft_login.exe`, `dist\jarvis_spotify_login.exe`.

## Installing on a client machine

Copy from `dist\` to wherever you're installing it:
- `JARVIS.exe` (+ the three login helpers if you want them available)
- `.env` — **do not copy the developer's own `.env`** (it has live
  secrets); copy `.env.example`, rename it, and fill in that machine's own
  `GROQ_API_KEY` etc.
- `.certs\` — only if that machine turns out to need the TLS-inspection
  workaround (see above); generate it there, don't copy this machine's.

Everything else (`.google_cache\`, `.ms_cache.bin`, `.spotify_cache`,
`jarvis.log`, `%USERPROFILE%\JarvisWorkspace`) is created automatically on
first use, next to `JARVIS.exe`.

## What was actually verified (this build, this machine)

Built the spec above and ran the resulting `JARVIS.exe` **standalone, in an
empty folder with nothing else copied in** (no source .py files, no
`templates/` on disk — everything had to come from inside the frozen
bundle):

1. With no `.env` present: printed the clean
   `[J.A.R.V.I.S cannot start] GROQ_API_KEY is not set...` message to
   stderr and exited — no Python traceback.
2. With a `.env` containing a placeholder (invalid) `GROQ_API_KEY`: started
   normally, created `jarvis.log` next to the `.exe` (proving the
   frozen-path/chdir fix works), bound Flask to `127.0.0.1`, and served
   real HTTP traffic:
   - `GET /` returned the actual rendered `index.html` (200) — proves the
     bundled `templates/` data + Flask's frozen root_path resolution works.
   - `GET /api/auth/google/status` returned valid JSON
     (`{"configured": false, "connected": false}`) — proves
     `google_service.py` (and its googleapiclient/google-auth imports)
     loaded cleanly inside the frozen bundle.
   - `POST /api/speak` with plain text returned a real base64-encoded MP3
     (~34KB) — proves **edge-tts's async network TTS call works from
     inside the frozen executable**, including whatever TLS path it took
     (no `.certs/` bundle was present for this test, so this also
     incidentally confirms the app works fine on a network that doesn't
     need the TLS-inspection workaround at all, which is the common case).
   - Launching a second copy while the first was still running correctly
     refused with the existing "already listening on this port" message
     — the frozen build's own instance-collision guard works too.
   - No `pyautogui`/`Pillow` "unavailable" warnings in the log — the
     screen-automation dependency chain loaded fine in the frozen build.

**What was NOT verified (needs a human, or a real key):**
- A real Groq API key was not available in this environment — the actual
  `/api/command` tool-calling path (the LLM brain) was not exercised
  end-to-end against the frozen exe. It only differs from the dev
  (`python app.py`) path in how the process starts, not in any LLM logic,
  so risk here is low, but it's still unverified.
- Real Google/Microsoft/Spotify OAuth credentials were not available here
  — the actual interactive consent flows (`jarvis_google_login.exe` etc.)
  were not run end-to-end. The build/import step succeeded for all three,
  which is the part most likely to break from freezing; the OAuth network
  round-trip itself is standard `requests`/`msal`/`google-auth-oauthlib`
  code unchanged from the unfrozen version.
- **This was all tested on the one machine this was built on.** A
  different Windows machine (different Windows version, different
  installed-software baseline, different antivirus) is the actual target
  per the brief, and was not available to test against. See the main
  report for the prioritized list of what still needs real multi-machine
  testing.
