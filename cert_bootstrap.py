"""
cert_bootstrap.py — local fix for a machine-specific SSL trust mismatch.

This machine's antivirus/network does TLS inspection with a root CA that
Windows itself already trusts, but Python's bundled `certifi` package
(used by requests, httplib2, urllib3, ...) does not — every outbound
HTTPS call from Python (pip installs, Google/Microsoft OAuth token
exchange, Graph/Calendar/Gmail API calls) fails with
CERTIFICATE_VERIFY_FAILED as a result.

The global fix would be replacing certifi's own cacert.pem in
site-packages, but that changes TLS trust for every Python program on
this machine, not just JARVIS — too broad a change to make silently.
Instead, this patches `certifi.where()` for THIS PROCESS ONLY, so only
code that explicitly imports cert_bootstrap first is affected. It's inert
everywhere else (falls back to plain certifi) and disappears the moment
this process exits — nothing durable or system-wide changes.

.certs/combined_ca_bundle.pem = certifi's own CA list + the CAs already
in the Windows Root store, exported once via PowerShell
(Get-ChildItem Cert:\\LocalMachine\\Root / Cert:\\CurrentUser\\Root).
Regenerate it if this ever goes stale (e.g. after an AV/proxy cert
rotation) — see README.

Import this before anything that makes HTTPS calls: google_service,
microsoft_service, groq, edge_tts, requests, spotipy.
"""

import os
import sys

# When frozen by PyInstaller, __file__ for a bundled module resolves inside
# the onefile build's temporary extraction directory (sys._MEIPASS), which
# is wiped when the process exits — a `.certs/` folder placed there would
# either be invisible (if not explicitly bundled as data) or, worse, a
# stale read-only copy baked in at build time instead of the real one next
# to the .exe. Anchor to sys.executable's folder instead when frozen, same
# as app.py's own _APP_DIR resolution, so this always looks next to the
# actual installed app regardless of how it's launched.
if getattr(sys, "frozen", False):
    _BASE_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    _BASE_DIR = os.path.dirname(os.path.abspath(__file__))

_BUNDLE_PATH = os.path.join(_BASE_DIR, ".certs", "combined_ca_bundle.pem")

if os.path.exists(_BUNDLE_PATH):
    os.environ.setdefault("SSL_CERT_FILE", _BUNDLE_PATH)
    os.environ.setdefault("REQUESTS_CA_BUNDLE", _BUNDLE_PATH)
    os.environ.setdefault("CURL_CA_BUNDLE", _BUNDLE_PATH)
    try:
        import certifi
        certifi.where = lambda: _BUNDLE_PATH
        certifi.core.where = lambda: _BUNDLE_PATH
    except Exception:
        pass
