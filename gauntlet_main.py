"""
Entry point for the Gauntlet Loop GUI.

Place this file in the same folder as app.py (the existing JARVIS project),
alongside a `gauntlet/` subfolder, then run:

    python gauntlet_main.py

Requires GROQ_API_KEY in your .env (same one app.py already uses). This
does NOT start the Flask server or the voice HUD - it's a separate desktop
GUI for autonomous multi-step tasks. You can run either or both.
"""

from gauntlet.gui import main

if __name__ == "__main__":
    main()
