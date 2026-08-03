"""
vision_action.py — general "computer use" loop.

Replaces per-service tools (draw_shape_in_paint, etc.) with one general
capability: look at the screen, decide one action, do it, look again.

Loop shape:
    screenshot -> vision LLM (goal + short action history + image)
               -> one JSON action {type, ...}
               -> execute via pyautogui
               -> screenshot again -> did the model say "done"? stop. else repeat.

Kept deliberately dumb, single-step-at-a-time: the model never gets to plan
five actions ahead from one screenshot, because screenshots go stale the
moment something moves. One look, one action, re-check.
"""

import base64
import json
import logging
import re
import os
import time
import uuid

from groq import RateLimitError

# Screen-automation deps. On Windows (this app's only real target) these
# are effectively always installable, but guard them the same way app.py
# guards pyautogui/pywhatkit/spotipy — a missing/broken native dependency
# should disable computer_use with a clear spoken message, not take down
# the whole Flask process at import time, since app.py imports this module
# unconditionally at startup.
try:
    import pyautogui
    from PIL import Image
    VISION_DEPS_AVAILABLE = True
except Exception as _e:  # pragma: no cover — exercised only when deps are missing
    pyautogui = None
    Image = None
    VISION_DEPS_AVAILABLE = False
    logging.getLogger("jarvis.vision_action").error(
        f"pyautogui/Pillow unavailable — computer_use will be disabled: {_e}"
    )

try:
    import pygetwindow as gw
except ImportError:  # pragma: no cover — only relevant off-Windows
    gw = None

import cursor_indicator
from guardrails import WORKSPACE_DIR, resolve_safe_path, ensure_workspace

logger = logging.getLogger("jarvis.vision_action")

# Never turn this off. Slamming the mouse into a screen corner is the
# emergency stop for anything pyautogui is doing.
if VISION_DEPS_AVAILABLE:
    pyautogui.FAILSAFE = True

# Vision-capable model on Groq as of writing — confirmed against
# https://console.groq.com/docs/vision as the generally-available option.
# (qwen/qwen3-vl-32b-instruct, tried earlier, returns a 404 for non-enterprise
# accounts — it's gated. Groq's lineup moves fast, so re-check that page if
# this starts 404ing again.)
VISION_MODEL_NAME = os.environ.get("JARVIS_VISION_MODEL", "qwen/qwen3.6-27b")

MAX_LOOP_STEPS = 8
STEP_SETTLE_SECONDS = 0.6  # let the UI finish animating before the next screenshot

ACTION_SCHEMA_PROMPT = """\
You are the planning component of a computer-use agent. You are given a goal, \
a screenshot of the current screen, and the actions taken so far. Respond with \
EXACTLY ONE JSON object (no markdown fences, no prose) describing the single \
next action. Schema:

{"type": "click", "x": <int>, "y": <int>, "button": "left"|"right", "reason": "<why>"}
{"type": "double_click", "x": <int>, "y": <int>, "reason": "<why>"}
{"type": "move", "x": <int>, "y": <int>, "reason": "<why>"}
{"type": "drag", "from_x": <int>, "from_y": <int>, "to_x": <int>, "to_y": <int>, "reason": "<why>"}
{"type": "type", "text": "<string to type>", "reason": "<why>"}
{"type": "key", "keys": ["ctrl", "s"], "reason": "<why>"}
{"type": "scroll", "amount": <int, positive=up>, "reason": "<why>"}
{"type": "wait", "seconds": <float, max 3>, "reason": "<why>"}
{"type": "done", "summary": "<what was accomplished>"}
{"type": "failed", "reason": "<why this can't be completed>"}

Coordinates are pixels on the screenshot you're shown, origin top-left.
x and y (and from_x/from_y/to_x/to_y) are each a single plain number —
NEVER an array like "x": [444, 969]. Always two separate keys, e.g.
{"type": "click", "x": 444, "y": 969, "button": "left", "reason": "..."}.
PREFER keyboard shortcuts over clicking when a reliable one exists — a key
press doesn't depend on pixel-perfect coordinates the way a click does.
Common ones: space toggles play/pause in Spotify and most media apps once
the window has focus; ctrl+f for in-app search; enter to submit/confirm.
If the app window is already focused (e.g. you just opened or clicked into
it), use {"type": "key", "keys": ["space"]} to play/pause rather than
hunting for the play button's coordinates. Only fall back to click when no
shortcut applies.
Only ever emit ONE action. Use "done" as soon as the goal is visibly achieved —
don't keep acting after that. Use "failed" if you're stuck after a few tries
rather than repeating the same action.
"""


# Groq's on-demand tier caps qwen/qwen3.6-27b at a low tokens-per-minute
# budget (seen in testing: 8000 TPM). A raw 1920x1080 screenshot alone can
# burn most of that in one call, so the multi-step loop gets 429'd after
# 1-2 steps before it ever finishes. Downscaling + JPEG-compressing what's
# sent to the model cuts token cost roughly 5-10x — the actual mouse/
# keyboard actions still execute at full real screen resolution, since the
# scale factor (real resolution / this reduced size) converts the model's
# coordinates back up before any pyautogui call.
VISION_IMAGE_MAX_DIM = int(os.environ.get("JARVIS_VISION_MAX_DIM", "800"))
VISION_JPEG_QUALITY = int(os.environ.get("JARVIS_VISION_JPEG_QUALITY", "70"))

if VISION_DEPS_AVAILABLE:
    try:
        _RESAMPLE = Image.Resampling.LANCZOS  # Pillow >= 9.1
    except AttributeError:  # pragma: no cover — older Pillow
        _RESAMPLE = Image.LANCZOS
else:
    _RESAMPLE = None


def take_screenshot() -> tuple[str, str, int, int]:
    """Capture the screen, downscale + JPEG-compress it for the vision
    model (to fit Groq's token budget), save under the workspace, and
    return (path, base64, sent_image_width, sent_image_height)."""
    ensure_workspace()
    fname = f"screen_{uuid.uuid4().hex[:8]}.jpg"
    path = resolve_safe_path(os.path.join("tmp", fname))
    img = pyautogui.screenshot()

    long_side = max(img.width, img.height)
    if long_side > VISION_IMAGE_MAX_DIM:
        factor = VISION_IMAGE_MAX_DIM / long_side
        img = img.resize((max(1, int(img.width * factor)), max(1, int(img.height * factor))), _RESAMPLE)

    img = img.convert("RGB")  # JPEG has no alpha channel
    img.save(path, format="JPEG", quality=VISION_JPEG_QUALITY)
    with open(path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("utf-8")
    return path, b64, img.width, img.height


def _scale_factors(screenshot_w: int, screenshot_h: int) -> tuple[float, float]:
    """Windows display scaling (125%/150%/etc.) means a screenshot's pixel
    size and pyautogui's logical coordinate space often don't match. The
    vision model only ever sees screenshot pixels, so its (x, y) values need
    converting into pyautogui's logical space before any mouse action, or
    every click lands in the wrong place.
    """
    logical_w, logical_h = pyautogui.size()
    if not screenshot_w or not screenshot_h:
        return 1.0, 1.0
    return logical_w / screenshot_w, logical_h / screenshot_h


def _ask_vision_model(groq_client, goal: str, screenshot_b64: str, action_history: list) -> dict:
    history_text = "\n".join(
        f"{i+1}. {a.get('type')} — {a.get('reason', a.get('summary', ''))}"
        for i, a in enumerate(action_history)
    ) or "(none yet)"

    messages = [
        {"role": "system", "content": ACTION_SCHEMA_PROMPT},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": f"Goal: {goal}\n\nActions so far:\n{history_text}"},
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{screenshot_b64}"},
                },
            ],
        },
    ]

    try:
        completion = groq_client.chat.completions.create(
            model=VISION_MODEL_NAME,
            messages=messages,
            temperature=0.2,
            max_tokens=300,
            # Qwen 3.6 27B is a hybrid reasoning model that "thinks out loud"
            # in a <think>...</think> block by default. We want the raw
            # action JSON only, so turn thinking off. (This is the one
            # family-specific case where Groq actually honors a full
            # disable — see console.groq.com/docs/reasoning.)
            reasoning_effort="none",
            # Constrains decoding to syntactically valid JSON. This is a
            # different problem from the [x, y]-packed-into-one-field issue
            # _normalize_action fixes below — that's valid JSON with the
            # wrong shape; without this setting we also saw outright broken
            # JSON syntax (e.g. '"x": 444, 969,') that json.loads can never
            # recover from no matter how it's normalized afterward.
            response_format={"type": "json_object"},
        )
    except RateLimitError as e:
        logger.error(f"Vision model rate limit hit ({VISION_MODEL_NAME}): {e}")
        return {"type": "failed", "reason": "I'm out of vision-model capacity for now, sir — try again shortly."}
    except Exception as e:
        # Raw exception text (often a JSON error blob from the API) is fine
        # in the log, but gets spoken aloud verbatim if passed straight
        # through here — keep this one client-presentable.
        logger.error(f"Vision model call failed ({VISION_MODEL_NAME}): {e}")
        return {"type": "failed", "reason": "I couldn't process that visually, sir — something went wrong on my end."}

    raw = completion.choices[0].message.content or "{}"
    # Belt-and-suspenders: some responses still include a <think> block even
    # with reasoning disabled, so strip it before looking for JSON.
    raw = re.sub(r"<think>.*?(</think>|$)", "", raw, flags=re.DOTALL).strip()
    raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    # The model may still wrap the JSON in a sentence or two — pull out the
    # first {...} block rather than requiring the whole string to be JSON.
    match = re.search(r"\{.*\}", raw, flags=re.DOTALL)
    if match:
        raw = match.group(0)
    try:
        return _normalize_action(json.loads(raw))
    except json.JSONDecodeError:
        logger.warning(f"Vision model returned non-JSON, treating as failure: {raw[:200]!r}")
        return {"type": "failed", "reason": "Vision model returned an unparseable response."}


# Field pairs that should be two separate numbers, keyed by action type.
_COORD_PAIRS = {
    "click": [("x", "y")],
    "double_click": [("x", "y")],
    "move": [("x", "y")],
    "drag": [("from_x", "from_y"), ("to_x", "to_y")],
}


def _coerce_number(value):
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            pass
    return value


def _normalize_action(action: dict) -> dict:
    """Repair the malformed shapes the vision model produces in practice,
    seen directly in testing:
      - packing [x, y] into a single "x" field with no separate "y" at all
      - sending coordinates as strings ("290") instead of numbers, which
        pyautogui.click() would otherwise misread as an image filename to
        locate on screen rather than a coordinate
    Runs on every parsed action before it's handed to the executor.
    """
    action = dict(action)
    kind = action.get("type")

    for x_key, y_key in _COORD_PAIRS.get(kind, []):
        x_val = action.get(x_key)
        if isinstance(x_val, (list, tuple)) and len(x_val) == 2:
            action[x_key], action[y_key] = x_val[0], x_val[1]

    for key in ("x", "y", "from_x", "from_y", "to_x", "to_y"):
        if key in action:
            action[key] = _coerce_number(action[key])

    return action


def _execute_action(action: dict, scale: tuple[float, float] = (1.0, 1.0)) -> None:
    sx, sy = scale

    def conv(x, y):
        if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
            raise ValueError(f"Non-numeric coordinate after normalization: x={x!r}, y={y!r}")
        return x * sx, y * sy

    kind = action.get("type")
    if kind == "click":
        x, y = conv(action["x"], action["y"])
        pyautogui.click(x, y, button=action.get("button", "left"))
    elif kind == "double_click":
        x, y = conv(action["x"], action["y"])
        pyautogui.doubleClick(x, y)
    elif kind == "move":
        x, y = conv(action["x"], action["y"])
        pyautogui.moveTo(x, y, duration=0.2)
    elif kind == "drag":
        fx, fy = conv(action["from_x"], action["from_y"])
        tx, ty = conv(action["to_x"], action["to_y"])
        pyautogui.moveTo(fx, fy)
        pyautogui.dragTo(tx, ty, duration=0.4, button="left")
    elif kind == "type":
        pyautogui.write(action.get("text", ""), interval=0.02)
    elif kind == "key":
        pyautogui.hotkey(*action.get("keys", []))
    elif kind == "scroll":
        pyautogui.scroll(int(action.get("amount", 0)))
    elif kind == "wait":
        time.sleep(min(float(action.get("seconds", 1)), 3.0))
    else:
        raise ValueError(f"Unknown or terminal action type in executor: {kind}")


def vision_action_loop(groq_client, goal: str, on_step=None) -> str:
    """Run the screenshot -> decide -> act loop until the model says the
    goal is done/failed or MAX_LOOP_STEPS is hit. `on_step(step_info)` is an
    optional callback (e.g. to push a line into the HUD terminal log).
    """
    if not VISION_DEPS_AVAILABLE:
        return "Screen automation isn't available on this system, sir — pyautogui/Pillow failed to load."
    cursor_indicator.set_active_cursor()
    try:
        return _run_loop(groq_client, goal, on_step)
    finally:
        cursor_indicator.restore_cursor()


def _try_focus_window(goal: str) -> None:
    """Best-effort: if the goal mentions an app that already has a window
    open (even minimized/background), bring it to the foreground before the
    loop starts. This is what was actually causing most of the "clicked the
    taskbar icon but nothing happened" loops in testing — the target app
    was already running but unfocused, so the model had to blindly guess
    the taskbar icon's pixel position on every attempt, and keyboard input
    like space silently went to whichever window really had focus instead.
    """
    if gw is None:
        return
    try:
        titles = [t for t in gw.getAllTitles() if t.strip()]
    except Exception as e:
        logger.warning(f"Couldn't enumerate windows to pre-focus: {e}")
        return

    goal_lower = goal.lower()
    for title in titles:
        # crude but effective: does a meaningful word from this window's
        # title show up in the goal text? ("Spotify Premium" ~ "Spotify")
        if any(word.lower() in goal_lower for word in title.split() if len(word) > 3):
            try:
                win = gw.getWindowsWithTitle(title)[0]
                if win.isMinimized:
                    win.restore()
                win.activate()
                logger.info(f"Pre-focused window '{title}' based on the goal text.")
                time.sleep(0.5)  # give Windows a moment to actually switch focus
                return
            except Exception as e:
                logger.warning(f"Found window '{title}' but couldn't focus it: {e}")
    logger.info("No existing window matched the goal text to pre-focus; proceeding as-is.")


def _run_loop(groq_client, goal: str, on_step=None) -> str:
    _try_focus_window(goal)
    history = []
    scale_logged = False
    for step in range(1, MAX_LOOP_STEPS + 1):
        _, screenshot_b64, shot_w, shot_h = take_screenshot()
        scale = _scale_factors(shot_w, shot_h)
        if not scale_logged:
            logger.info(
                f"Screenshot {shot_w}x{shot_h} vs pyautogui logical {pyautogui.size()} "
                f"-> scale factors {scale}"
            )
            scale_logged = True

        action = _ask_vision_model(groq_client, goal, screenshot_b64, history)
        kind = action.get("type")

        if kind == "done":
            summary = action.get("summary", "Goal achieved.")
            if on_step:
                on_step({"step": step, "type": "done", "detail": summary})
            return summary

        if kind == "failed":
            reason = action.get("reason", "Unable to complete the goal.")
            if on_step:
                on_step({"step": step, "type": "failed", "detail": reason})
            return f"I couldn't complete that, sir: {reason}"

        try:
            _execute_action(action, scale=scale)
        except Exception as e:
            logger.error(f"Action execution failed: {action} -> {e}")
            history.append({"type": "error", "reason": f"execution error: {e}"})
            if on_step:
                on_step({"step": step, "type": "error", "detail": str(e)})
            continue

        history.append(action)
        if on_step:
            on_step({"step": step, "type": kind, "detail": action.get("reason", "")})
        time.sleep(STEP_SETTLE_SECONDS)

    return "I stopped after reaching the step limit without confirming the goal was met, sir."
