# -*- mode: python ; coding: utf-8 -*-
# JARVIS PyInstaller build spec.
#
# Builds four onefile Windows executables from this project:
#   - JARVIS.exe            the actual app (Flask server + voice HUD)
#   - jarvis_google_login.exe    one-time Google OAuth setup helper
#   - jarvis_microsoft_login.exe one-time Microsoft OAuth setup helper
#   - jarvis_spotify_login.exe   one-time Spotify OAuth setup helper
#
# Usage (from the project root, i.e. the folder containing app.py):
#   pip install pyinstaller
#   pyinstaller packaging/jarvis.spec --distpath dist --workpath build --clean
#
# Output lands in dist/ as standalone .exe files. Copy them (NOT the dev
# .py sources) to wherever you're installing JARVIS, alongside:
#   - .env               (copy from .env.example and fill in — see README)
#   - .certs/             (only needed if this specific machine's AV/proxy
#                          does TLS inspection — see README's SSL section;
#                          generated ON that machine, never copied from here)
# The exes create .google_cache/, .ms_cache.bin, .spotify_cache, jarvis.log,
# and ~/JarvisWorkspace on their own the first time each feature is used.
#
# WHY these specific choices:
#   - console=True (not windowed): this app logs meaningfully to stdout
#     (Flask request logs, tool-call traces, the friendly startup error
#     messages app.py/guardrails.py now raise via SystemExit) and a first
#     "it doesn't work" report is far easier to diagnose with that console
#     visible than with nothing but a silently-closing window.
#   - onefile: simplest to hand to a client as a single .exe; the tradeoff
#     (slower cold start — unpacks to a temp dir every launch — and each
#     exe being fairly large since Groq/Google/MSAL/pyautogui/Pillow all
#     ship their own dependency trees) is acceptable for a desktop
#     assistant that's launched once and left running, not relaunched
#     constantly.
#   - templates/ is bundled as data (Flask's root_path auto-resolves to the
#     frozen bundle's extraction dir via __name__/__file__, same as it
#     resolves relative to app.py's own folder unfrozen) so render_template
#     keeps working without code changes.
#   - .certs/ is deliberately NOT bundled: it's a machine-specific TLS
#     trust workaround (this dev machine's antivirus root CA), wrong -- or
#     at best a no-op -- on any other machine. cert_bootstrap.py already
#     degrades gracefully (falls back to plain certifi) if the file isn't
#     present next to the exe, so shipping without it is the correct
#     default, not an oversight.

import sys
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

PROJECT_ROOT = Path(SPECPATH).parent  # packaging/jarvis.spec -> project root

block_cipher = None

# googleapiclient ships its calendar/gmail API discovery documents as
# package data (googleapiclient/discovery_cache/documents/*.json) — without
# these, build("calendar", "v3", ...) / build("gmail", "v1", ...) fail at
# runtime with a discovery-document-not-found error even though the import
# itself succeeded silently at build time.
datas = [
    (str(PROJECT_ROOT / "templates"), "templates"),
    (str(PROJECT_ROOT / ".env.example"), "."),
]
datas += collect_data_files("googleapiclient")
datas += collect_data_files("edge_tts")
datas += collect_data_files("certifi")

# Packages that use dynamic/plugin-style imports PyInstaller's static
# bytecode scan can miss. edge_tts's drm/subtitle submodules and aiohttp's
# optional speedup extensions are the ones actually observed to need this;
# the rest are cheap insurance for the same class of problem in libraries
# this app depends on for calendar/mail/voice/automation.
hiddenimports = (
    collect_submodules("googleapiclient")
    + collect_submodules("google_auth_oauthlib")
    + collect_submodules("edge_tts")
    + [
        "engineio.async_drivers.threading",
        "pyscreeze",
        "pymsgbox",
        "pytweening",
        "mouseinfo",
        "pygetwindow",
        "pygetwindow._pygetwindow_win",
        "PIL._tkinter_finder",
    ]
)

a = Analysis(
    [str(PROJECT_ROOT / "app.py")],
    pathex=[str(PROJECT_ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

# --- Login helpers (share the same Analysis graph so common deps aren't
# collected three extra times) --------------------------------------------
a_google = Analysis(
    [str(PROJECT_ROOT / "google_login.py")],
    pathex=[str(PROJECT_ROOT)],
    datas=[(str(PROJECT_ROOT / ".env.example"), ".")],
    hiddenimports=collect_submodules("googleapiclient") + collect_submodules("google_auth_oauthlib"),
    cipher=block_cipher,
)
a_ms = Analysis(
    [str(PROJECT_ROOT / "microsoft_login.py")],
    pathex=[str(PROJECT_ROOT)],
    datas=[(str(PROJECT_ROOT / ".env.example"), ".")],
    hiddenimports=["msal"],
    cipher=block_cipher,
)
a_spotify = Analysis(
    [str(PROJECT_ROOT / "spotify_login.py")],
    pathex=[str(PROJECT_ROOT)],
    datas=[(str(PROJECT_ROOT / ".env.example"), ".")],
    hiddenimports=["spotipy"],
    cipher=block_cipher,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)
pyz_google = PYZ(a_google.pure, a_google.zipped_data, cipher=block_cipher)
pyz_ms = PYZ(a_ms.pure, a_ms.zipped_data, cipher=block_cipher)
pyz_spotify = PYZ(a_spotify.pure, a_spotify.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz, a.scripts, a.binaries, a.zipfiles, a.datas, [],
    name="JARVIS",
    console=True,
    onefile=True,
    clean=False,
)

exe_google = EXE(
    pyz_google, a_google.scripts, a_google.binaries, a_google.zipfiles, a_google.datas, [],
    name="jarvis_google_login",
    console=True,
    onefile=True,
)

exe_ms = EXE(
    pyz_ms, a_ms.scripts, a_ms.binaries, a_ms.zipfiles, a_ms.datas, [],
    name="jarvis_microsoft_login",
    console=True,
    onefile=True,
)

exe_spotify = EXE(
    pyz_spotify, a_spotify.scripts, a_spotify.binaries, a_spotify.zipfiles, a_spotify.datas, [],
    name="jarvis_spotify_login",
    console=True,
    onefile=True,
)
