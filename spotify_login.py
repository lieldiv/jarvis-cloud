"""
One-time Spotify login helper.

The first time app.py needs a Spotify token, it opens your browser to log
in and approve access, then caches the token in .spotify_cache for next
time. If that first login happens automatically during a live voice
command, the request just sits there waiting for you — fine, but easy to
mistake for JARVIS having frozen. Run this once beforehand instead so the
login happens on your terms, with output you can actually read.

Usage:
    python spotify_login.py
"""

import os

from dotenv import load_dotenv

load_dotenv()

client_id = os.environ.get("SPOTIFY_CLIENT_ID")
client_secret = os.environ.get("SPOTIFY_CLIENT_SECRET")
redirect_uri = os.environ.get("SPOTIFY_REDIRECT_URI", "http://127.0.0.1:8888/callback")

if not client_id or not client_secret:
    raise SystemExit(
        "SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET are not set in .env.\n"
        "Create an app at https://developer.spotify.com/dashboard first — see README.md."
    )

import spotipy
from spotipy.oauth2 import SpotifyOAuth

auth_manager = SpotifyOAuth(
    client_id=client_id,
    client_secret=client_secret,
    redirect_uri=redirect_uri,
    scope="user-modify-playback-state user-read-playback-state",
    cache_path=".spotify_cache",
    open_browser=True,
)

sp = spotipy.Spotify(auth_manager=auth_manager)
me = sp.me()
print(f"Logged in as: {me.get('display_name') or me.get('id')}")
print("Token cached in .spotify_cache — app.py will reuse it automatically.")
