"""
One-time Google login helper (Calendar + Gmail).

Same reasoning as spotify_login.py: the first time app.py needed a Google
token it would open your browser and block mid-request, which looks like
JARVIS froze. Run this once beforehand instead.

Usage:
    python google_login.py
"""

import os

from dotenv import load_dotenv

load_dotenv()

if not (os.environ.get("GOOGLE_CLIENT_ID") and os.environ.get("GOOGLE_CLIENT_SECRET")):
    raise SystemExit(
        "GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET are not set in .env.\n"
        "Create a Desktop app OAuth client at https://console.cloud.google.com/ "
        "first — see README.md."
    )

import google_service

if not google_service.GOOGLE_LIBS_AVAILABLE:
    raise SystemExit(
        "Google API libraries aren't installed. Run: "
        "pip install google-auth google-auth-oauthlib google-api-python-client"
    )

creds = google_service.get_credentials(interactive=True)
if not creds:
    raise SystemExit("Google login failed — see the error above.")

print("Google login successful.")
print(f"Token cached in {google_service.TOKEN_PATH} — app.py will reuse it automatically.")
