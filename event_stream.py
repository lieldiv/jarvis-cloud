"""
event_stream.py — a tiny in-process pub/sub used to push live progress
events (vision-action steps, self-heal attempts, confirmation requests) to
the HUD over Server-Sent Events, instead of the HUD polling an endpoint.

SSE rather than a full WebSocket: one-directional (backend -> HUD) is all
this needs, it's plain HTTP so nothing new to install (no flask-socketio,
no separate ASGI server), and the browser's built-in EventSource handles
reconnects for you. If you later want the HUD to push things back over the
same connection (vs. the existing POST /api/command), swap this for
flask-socketio without changing anything in vision_action.py or
self_healing.py — they only ever call push_event().
"""

import json
import queue
import threading

_subscribers = []
_lock = threading.Lock()

# Bounded so a stalled/slow HUD tab can't leak memory into an ever-growing
# queue — it just starts dropping the oldest progress events for that tab.
_QUEUE_MAXSIZE = 200


def push_event(event: dict):
    """Fire-and-forget broadcast to every connected HUD tab. Safe to call
    from any thread, including from inside the Groq tool-call handlers.
    """
    with _lock:
        subs = list(_subscribers)
    for q in subs:
        try:
            q.put_nowait(event)
        except queue.Full:
            try:
                q.get_nowait()  # drop oldest, make room for the newest
                q.put_nowait(event)
            except queue.Empty:
                pass


def subscribe() -> "queue.Queue":
    q = queue.Queue(maxsize=_QUEUE_MAXSIZE)
    with _lock:
        _subscribers.append(q)
    return q


def unsubscribe(q: "queue.Queue"):
    with _lock:
        if q in _subscribers:
            _subscribers.remove(q)


def format_sse(event: dict) -> str:
    return f"data: {json.dumps(event)}\n\n"
