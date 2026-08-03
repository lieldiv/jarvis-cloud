# Gauntlet Loop add-on

Adds a second, independent entry point to your existing JARVIS project: a
PySide6 desktop GUI that runs a multi-step, self-correcting agent loop
(Builder -> execute -> Critic -> retry/continue) on top of the same tools
your Flask assistant already has (weather, open/close apps, web search,
YouTube/Spotify playback, Paint drawing, calculator) plus one new
allowlisted command-runner tool.

## Install

Copy `gauntlet/` (the whole folder) and `gauntlet_main.py` into your
existing project folder, next to `app.py`. Then:

```bash
pip install -r requirements.txt
pip install -r requirements-gauntlet.txt
```

Your existing `.env` (with `GROQ_API_KEY`, and optionally the Spotify
credentials) is reused as-is - nothing new to configure there.

## Run

```bash
python gauntlet_main.py
```

This opens a separate window from the Flask/voice HUD - it's not a
replacement for `app.py`, you can run either or both. Type a goal (e.g.
"play Bohemian Rhapsody on Spotify"), pick a max-rounds cap, hit Run.

## How the loop works

```
Builder proposes ONE tool call
        |
        v
   tool executes  ---->  observation (text)
        |
        v
Critic reviews (goal, action, observation)
   -> PASS      : goal achieved, stop
   -> RETRY     : that step failed, feedback goes back to Builder
   -> CONTINUE  : reasonable partial progress, keep going
        |
        v
   loop again (or stop at max_rounds)
```

Every round is logged live in the GUI: the Builder's chosen tool + args,
the raw observation, and the Critic's verdict + feedback, so you can watch
the self-correction happen instead of it being a black box.

`task_complete` is a tool the Builder can call itself when it believes the
goal is done; the Critic still gets a chance to disagree on the final
round before the loop reports success.

## Files

- `gauntlet/config.py` - Groq client setup, reuses `GROQ_API_KEY` from `.env`.
- `gauntlet/tools.py` - imports the existing tool implementations from
  `app.py` (no duplicated logic) and adds `run_shell_command` (allowlisted
  argv-only executor) and `task_complete`.
- `gauntlet/agents.py` - `BuilderAgent` (proposes the next tool call) and
  `CriticAgent` (judges the result, PASS/CONTINUE/RETRY).
- `gauntlet/loop.py` - `GauntletLoop`, the orchestrator described above.
- `gauntlet/gui.py` - PySide6 window; runs the loop on a `QThread` so the
  UI never freezes, and surfaces every step as a log line via Qt signals.
- `gauntlet_main.py` - the file you actually run.

## Safety notes (read before extending this)

- **`run_shell_command` only runs allowlisted programs** (see
  `ALLOWED_COMMANDS` in `gauntlet/tools.py`: `dir`, `whoami`, `ping`,
  `tasklist`, `ipconfig`, `python`, `pip`, etc.) and always as an argv list
  with `shell=False` - shell metacharacters in arguments are inert, the
  same protection `app.py` already applies to `open_application`. If you
  add entries, add them one at a time and think about what that program
  can do with your user's permissions.
- **There's no human-in-the-loop approval step** between the Builder
  deciding on an action and it executing - that's the nature of an
  autonomous loop. The safety boundary is the tool allowlist and
  `max_rounds`, not a confirmation dialog. If you want a "confirm before
  each action" mode, the natural place to add it is in
  `gauntlet/loop.py`, right before `gtools.execute_tool_call(...)` is
  called - emit an event, block on a GUI response, then proceed or skip.
- **Paint drawing** still depends on the pixel-coordinate calibration from
  the base project (`calibrate_paint.py`) - the Gauntlet loop doesn't
  change that limitation.
- **Spotify playback** still requires Premium + an active device, same as
  in `app.py`.
- The Critic is itself an LLM call and can be wrong (approve something
  that didn't really work, or reject something that did). Treat its
  verdict as a heuristic, not a guarantee - `max_rounds` is there so a
  disagreement between Builder and Critic can't loop forever.
