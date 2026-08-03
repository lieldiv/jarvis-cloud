"""
Calibration helper for the draw_shape_in_paint tool.

app.py's Paint automation clicks and drags at RELATIVE (0.0-1.0) coordinates
within the maximized Paint window (PAINT_ELLIPSE_TOOL_REL and
PAINT_CANVAS_START_REL). Those defaults are a starting guess and will very
likely need adjusting for your Windows/Paint version and DPI scaling — this
script makes that quick.

Usage:
    1. Open Paint yourself and maximize the window.
    2. Run: python calibrate_paint.py
    3. Hover your mouse over the Ellipse tool in the ribbon and read the
       "fraction" numbers it prints.
    4. Do the same hovering over a spot inside the canvas.
    5. Copy those (x, y) fractions into PAINT_ELLIPSE_TOOL_REL and
       PAINT_CANVAS_START_REL near the top of app.py.
    6. Press Ctrl+C to stop.
"""

import time

import pyautogui

print(f"Screen size: {pyautogui.size().width} x {pyautogui.size().height}")
print("Move your mouse over the Ellipse tool, then over the canvas.")
print("Reading updates live below. Press Ctrl+C to stop.\n")

try:
    while True:
        x, y = pyautogui.position()
        w, h = pyautogui.size()
        print(f"\rpixels: ({x:5d}, {y:5d})   fraction: ({x / w:.3f}, {y / h:.3f})   ", end="", flush=True)
        time.sleep(0.15)
except KeyboardInterrupt:
    print("\nDone — plug the fraction values into app.py.")
