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

MULTI-USER BUILD: subscribers are now keyed by user_id. The original
single-tenant version broadcast every event to every connected tab — fine
with one account, but in the multi-user build it meant one user's proposed
calendar event, drafted email, or self-heal progress was pushed to every
*other* signed-in user's browser too. push_event() now requires a user_id
and only reaches that user's own subscribed tab(s).
"""

import json
import queue
import threading

_subscribers = {}  # user_id -> list[queue.Queue]
_lock = threading.Lock()

# Bounded so a stalled/slow HUD tab can't leak memory into an ever-growing
# queue — it just starts dropping the oldest progress events for that tab.
_QUEUE_MAXSIZE = 200


def push_event(event: dict, user_id: str):
    """Fire-and-forget delivery to every tab this specific user_id has open.
    Safe to call from any thread, including from inside the Groq tool-call
    handlers. user_id is required (not optional/defaulted) so a call site
    can't silently regress into broadcasting to everyone by forgetting it.
    """
    if not user_id:
        return
    with _lock:
        subs = list(_subscribers.get(user_id, ()))
    for q in subs:
        try:
            q.put_nowait(event)
        except queue.Full:
            try:
                q.get_nowait()  # drop oldest, make room for the newest
                q.put_nowait(event)
            except queue.Empty:
                pass


def subscribe(user_id: str) -> "queue.Queue":
    q = queue.Queue(maxsize=_QUEUE_MAXSIZE)
    with _lock:
        _subscribers.setdefault(user_id, []).append(q)
    return q


def unsubscribe(user_id: str, q: "queue.Queue"):
    with _lock:
        subs = _subscribers.get(user_id)
        if subs and q in subs:
            subs.remove(q)
            if not subs:
                del _subscribers[user_id]


def format_sse(event: dict) -> str:
    return f"data: {json.dumps(event)}\n\n"
