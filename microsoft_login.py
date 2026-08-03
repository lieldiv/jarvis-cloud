"""
One-time Microsoft (Outlook/365) login helper.

Same reasoning as spotify_login.py / google_login.py: run this once so the
interactive consent screen happens on your terms, not mid voice-command.

Usage:
    python microsoft_login.py
"""

import os

from dotenv import load_dotenv

load_dotenv()

if not os.environ.get("MS_CLIENT_ID"):
    raise SystemExit(
        "MS_CLIENT_ID is not set in .env.\n"
        "Register an app at https://portal.azure.com/ first — see README.md."
    )

import microsoft_service

if not microsoft_service.MSAL_AVAILABLE:
    raise SystemExit("msal isn't installed. Run: pip install msal")

token = microsoft_service.get_access_token(interactive=True)
if not token:
    raise SystemExit("Microsoft login failed — see the error above.")

print("Microsoft login successful.")
print(f"Token cached in {microsoft_service.CACHE_PATH} — app.py will reuse it automatically.")
