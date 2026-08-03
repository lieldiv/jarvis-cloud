# Wiring the new modules into `app.py`

Drop `guardrails.py`, `vision_action.py`, `self_healing.py`, and
`file_tools.py` into the project root, next to `app.py`. Add `psutil` to
`requirements.txt`. Then:

## 1. Startup — refuse to run elevated, ensure the workspace exists

At the very top of `app.py`, right after the existing imports:

```python
from guardrails import refuse_if_elevated, ensure_workspace
import file_tools, vision_action, self_healing

refuse_if_elevated()   # raises and exits if launched as admin/root
ensure_workspace()
```

This replaces nothing — it's a new pair of lines before the Groq client
setup. If someone double-clicks a "Run as administrator" shortcut, the
process exits immediately with a clear message instead of silently running
with more power than the sandbox checks assume.

## 2. New tools in `TOOLS` and `TOOL_IMPL`

Append to the existing `TOOLS` list (same schema as your current entries):

```python
{
    "type": "function",
    "function": {
        "name": "computer_use",
        "description": (
            "General screen control for anything without a dedicated tool: "
            "clicking UI elements, filling in forms, browser navigation, "
            "dragging, typing into any app. Describe the visual goal in "
            "plain language, e.g. 'open Gmail and click Compose'."
        ),
        "parameters": {
            "type": "object",
            "properties": {"goal": {"type": "string"}},
            "required": ["goal"],
        },
    },
},
{
    "type": "function",
    "function": {
        "name": "write_and_test_code",
        "description": (
            "Write a Python script to accomplish a goal, run it in the "
            "sandbox, and automatically fix and re-run it if it errors, "
            "until it succeeds or the retry limit is reached."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "goal": {"type": "string"},
                "initial_code": {"type": "string", "description": "First draft of the script"},
            },
            "required": ["goal", "initial_code"],
        },
    },
},
{
    "type": "function",
    "function": {
        "name": "list_workspace",
        "description": "List files in the JARVIS workspace (or a subfolder of it).",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
    },
},
{
    "type": "function",
    "function": {
        "name": "read_workspace_file",
        "description": "Read a text file from the JARVIS workspace.",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
},
{
    "type": "function",
    "function": {
        "name": "write_workspace_file",
        "description": "Write/overwrite a text file in the JARVIS workspace.",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"],
        },
    },
},
{
    "type": "function",
    "function": {
        "name": "delete_workspace_path",
        "description": "Delete a file or folder in the JARVIS workspace. Requires user confirmation before it actually happens.",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
},
```

And the matching implementations:

```python
def _computer_use(args):
    result = vision_action.vision_action_loop(groq_client, args.get("goal", ""))
    return result

def _write_and_test_code(args):
    outcome = self_healing.self_healing_code_loop(
        groq_client, MODEL_NAME, args.get("goal", ""), args.get("initial_code", "")
    )
    if outcome["success"]:
        return f"Succeeded after {outcome['attempts']} attempt(s), sir. Output: {outcome['stdout'][:300]}"
    return f"Couldn't get it working after {outcome['attempts']} attempts, sir. Last error: {outcome['stderr'][-300:]}"

def _delete_workspace_path(args):
    result = file_tools.delete_path(args.get("path", ""))
    if result["status"] == "confirmation_required":
        return f"Please confirm, sir: {result['message']} (token {result['token']})"
    return result["message"]

TOOL_IMPL.update({
    "computer_use": _computer_use,
    "write_and_test_code": _write_and_test_code,
    "list_workspace": lambda args: file_tools.list_dir(args.get("path", ".")),
    "read_workspace_file": lambda args: file_tools.read_file(args.get("path", "")),
    "write_workspace_file": lambda args: file_tools.write_file(args.get("path", ""), args.get("content", "")),
    "delete_workspace_path": _delete_workspace_path,
})
```

Note `write_and_test_code` and `computer_use` can each take several seconds
to tens of seconds (multiple screenshots or multiple sandbox runs) — the
existing `MAX_TOOL_ROUNDS` cap on chained tool calls still applies on top of
this, so a confused model can't compound a slow loop with more slow loops
indefinitely.

## 3. Confirmation endpoint

```python
from guardrails import resolve_confirmation, list_pending

@app.route("/api/confirm/<token>", methods=["POST"])
def confirm_action(token):
    approve = bool((request.json or {}).get("approve", False))
    message = resolve_confirmation(token, approve)
    return jsonify({"response": message})

@app.route("/api/pending", methods=["GET"])
def pending_confirmations():
    return jsonify(list_pending())
```

In the HUD, poll `/api/pending` (or just surface the token JARVIS speaks
back in its response) and show a confirm/cancel prompt that POSTs to
`/api/confirm/<token>`.

## 4. Streaming progress into the HUD's terminal log — SSE

`event_stream.py` is a small in-process pub/sub. `vision_action_loop` and
`self_healing_code_loop` both take an optional `on_step` / `on_attempt`
callback — point both at `event_stream.push_event` and every step becomes a
push to any connected HUD tab, no polling.

```python
import event_stream

def _computer_use(args):
    return vision_action.vision_action_loop(
        groq_client, args.get("goal", ""), on_step=event_stream.push_event
    )

def _write_and_test_code(args):
    outcome = self_healing.self_healing_code_loop(
        groq_client, MODEL_NAME, args.get("goal", ""), args.get("initial_code", ""),
        on_attempt=event_stream.push_event,
    )
    ...
```

Add the SSE route:

```python
from flask import Response
import queue as _queue

@app.route("/api/stream")
def stream():
    def gen():
        q = event_stream.subscribe()
        try:
            while True:
                try:
                    event = q.get(timeout=15)
                    yield event_stream.format_sse(event)
                except _queue.Empty:
                    yield ": keepalive\n\n"   # comment line, keeps proxies/browsers from timing out the connection
        finally:
            event_stream.unsubscribe(q)

    return Response(
        gen(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
```

**Important:** this is a long-lived connection, so the dev server must
handle more than one request at a time or `/api/stream` will block
`/api/command`. Change the bottom of `app.py`:

```python
app.run(host=host, port=port, debug=debug_mode, threaded=True)
```

Also push confirmation requests the same way, so the HUD can pop the
prompt the instant a delete/kill is requested rather than waiting for the
next `/api/command` reply to mention it — call
`event_stream.push_event({"type": "confirmation_required", "token": token, "message": message})`
right where `_delete_workspace_path` currently builds its return string.

On the HUD side, `EventSource` is built into every browser — no library:

```javascript
const stream = new EventSource("/api/stream");
stream.onmessage = (e) => {
  const event = JSON.parse(e.data);
  appendToTerminalLog(event);            // your existing scrolling log panel
  if (event.type === "confirmation_required") {
    showConfirmPrompt(event.token, event.message);   // POST to /api/confirm/<token>
  }
};
```

`EventSource` auto-reconnects if the connection drops (e.g. a Flask
autoreload restart in dev), so you don't need to handle that yourself.
