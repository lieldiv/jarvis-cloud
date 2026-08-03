"""
cursor_indicator.py — swaps the Windows cursor to a distinct shape while
JARVIS is actively controlling the mouse, and restores your normal cursor
right after. Purely cosmetic — a visual "I'm driving right now" signal so
you can tell at a glance whether it's you or JARVIS moving the mouse. Has
no effect on click accuracy or coordinates. Windows-only; a harmless no-op
on any other OS.
"""

import ctypes
import logging
import os

logger = logging.getLogger("jarvis.cursor_indicator")

_OCR_NORMAL = 32512   # resource id of the standard arrow cursor
_IDC_CROSS = 32515    # crosshair — visually distinct from the normal arrow
_SPI_SETCURSORS = 0x57  # restores the user's default cursor scheme

_active = False


def set_active_cursor():
    """Call when JARVIS starts driving the mouse."""
    global _active
    if os.name != "nt" or _active:
        return
    try:
        user32 = ctypes.windll.user32
        stock_handle = user32.LoadCursorW(None, _IDC_CROSS)
        # SetSystemCursor takes ownership of (and destroys) the handle it's
        # given, so hand it a copy rather than the shared stock cursor.
        owned_copy = user32.CopyIcon(stock_handle)
        if not user32.SetSystemCursor(owned_copy, _OCR_NORMAL):
            raise OSError("SetSystemCursor returned failure")
        _active = True
    except Exception as e:
        logger.warning(f"Couldn't set the JARVIS cursor (cosmetic only, continuing): {e}")


def restore_cursor():
    """Call when JARVIS is done, success or failure — always restores your
    normal cursor. Safe to call even if set_active_cursor() was never
    called or failed.
    """
    global _active
    if os.name != "nt" or not _active:
        return
    try:
        ctypes.windll.user32.SystemParametersInfoW(_SPI_SETCURSORS, 0, None, 0)
    except Exception as e:
        logger.warning(f"Couldn't restore your normal cursor: {e}")
    finally:
        _active = False
