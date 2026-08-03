"""
mouse_test.py — standalone sanity check for real mouse control, with NO
vision model, NO Flask, NO JARVIS involved. Just pyautogui by itself.

Run directly:
    py mouse_test.py

What it does:
  1. Prints pyautogui's logical screen size (what moveTo/click use) next to
     a screenshot's actual pixel size (what the vision model sees). If these
     don't match, Windows display scaling (125%/150%/etc.) is active, and
     that mismatch is very likely why computer_use's clicks miss — the
     model is choosing coordinates in screenshot-pixel-space, but pyautogui
     is moving the mouse in logical-space.
  2. Moves the actual mouse cursor to 5 clearly different spots (corners +
     center), 2 seconds apart, so you can watch it with your own eyes and
     confirm real mouse control works at all, independent of anything else.
"""

import time

import pyautogui

pyautogui.FAILSAFE = True


def main():
    logical_w, logical_h = pyautogui.size()
    print(f"pyautogui logical screen size : {logical_w} x {logical_h}")

    shot = pyautogui.screenshot()
    print(f"screenshot pixel size         : {shot.width} x {shot.height}")

    if (logical_w, logical_h) != (shot.width, shot.height):
        sx = logical_w / shot.width
        sy = logical_h / shot.height
        print("\n>>> MISMATCH DETECTED — Windows display scaling is almost certainly on.")
        print(f">>> Required scale factors: x={sx:.4f}, y={sy:.4f}")
        print(">>> This is the fix computer_use needs: multiply any (x, y) the")
        print(">>> vision model gives by these factors before calling pyautogui.")
    else:
        print("\nNo mismatch — 1:1 scaling, nothing special to compensate for.")

    points = {
        "top-left": (50, 50),
        "top-right": (logical_w - 50, 50),
        "center": (logical_w // 2, logical_h // 2),
        "bottom-left": (50, logical_h - 50),
        "bottom-right": (logical_w - 50, logical_h - 50),
    }

    print("\nMoving the mouse to 5 points, 2 seconds apart — watch your screen.")
    print("(Move your mouse to a screen corner at any time to abort — that's pyautogui's FAILSAFE.)")
    time.sleep(2)

    for name, (x, y) in points.items():
        print(f"  -> {name}: ({x}, {y})")
        pyautogui.moveTo(x, y, duration=0.5)
        time.sleep(2)

    print("\nDone. If the cursor visibly reached all 5 points in the right spots,")
    print("mouse control itself is fine — any remaining problem is the vision")
    print("model picking wrong coordinates, not pyautogui/mouse control.")


if __name__ == "__main__":
    main()
