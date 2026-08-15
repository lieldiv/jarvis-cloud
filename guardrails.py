"""
guardrails.py — the safety layer every new JARVIS capability goes through.

Nothing in vision_action.py, self_healing.py, or file_tools.py touches the
filesystem, a process, or the OS directly. They all call through here first.
That's deliberate: one audited module is easier to trust than "we remembered
to check" scattered across three files.

Three protections live here:
  1. Sandboxing      — resolve_safe_path() confines all file work to a single
                        JARVIS_WORKSPACE folder, with an explicit denylist as
                        defense-in-depth even inside it.
  2. No admin         — refuse_if_admin() stops the whole app from starting
                        if launched elevated.
  3. Confirm-to-act    — a pending-confirmation registry for anything
                        destructive (delete, kill process), surfaced to the
                        HUD instead of executed silently.
"""

from __future__ import annotations  # PEP 604 `X | None` hints, running on Python 3.9

import logging
import os
import secrets
import time

logger = logging.getLogger("jarvis.guardrails")

# ---------------------------------------------------------------------------
# 1. Sandboxing
# ---------------------------------------------------------------------------
# Everything JARVIS reads, writes, or executes as "its own" work lives here.
# Override with the JARVIS_WORKSPACE env var if you want it somewhere else,
# but it must never resolve to a system directory (checked below).
WORKSPACE_DIR = os.path.realpath(
    os.environ.get("JARVIS_WORKSPACE", os.path.join(os.path.expanduser("~"), "JarvisWorkspace"))
)

# Belt-and-suspenders: even if WORKSPACE_DIR were misconfigured, never allow
# operations whose resolved path touches these. Lowercased, matched as
# path-segment prefixes on the realpath.
_DENYLIST_SEGMENTS = (
    "windows", "system32", "syswow64", "program files", "program files (x86)",
    "programdata", "$recycle.bin", "boot", "recovery",
)


def _touches_denylist(real_path: str) -> bool:
    lowered = real_path.lower()
    return any(seg in lowered for seg in _DENYLIST_SEGMENTS)


def ensure_workspace():
    """Create the workspace (and a sandbox_scripts subfolder) if missing."""
    if _touches_denylist(WORKSPACE_DIR):
        raise SystemExit(
            f"\n[J.A.R.V.I.S cannot start]\n"
            f"JARVIS_WORKSPACE resolves to a protected system path ({WORKSPACE_DIR}).\n"
            "Refusing to start — point it somewhere under your user profile "
            "(set JARVIS_WORKSPACE in .env), then try again.\n"
        )
    os.makedirs(WORKSPACE_DIR, exist_ok=True)
    os.makedirs(os.path.join(WORKSPACE_DIR, "sandbox_scripts"), exist_ok=True)
    os.makedirs(os.path.join(WORKSPACE_DIR, "tmp"), exist_ok=True)


def _safe_user_segment(user_id: str) -> str:
    """user_id is Google's 'sub' claim — normally just digits — but treat it
    as untrusted input anyway rather than assuming: strip anything that
    isn't alphanumeric so it can never be used to escape the per-user
    directory it's about to become a path segment of."""
    cleaned = "".join(c for c in (user_id or "") if c.isalnum())
    if not cleaned:
        raise SandboxViolation("Missing or invalid user id.")
    return cleaned


def user_workspace_dir(user_id: str) -> str:
    """Multi-user build: every signed-in user gets their own subtree under
    WORKSPACE_DIR (users/<id>/) instead of all sharing WORKSPACE_DIR itself
    — without this, one user's write_workspace_file/write_and_test_code
    calls would be readable, overwritable, and deletable by every other
    signed-in user, since file_tools.py and self_healing.py have no other
    concept of "whose" file something is."""
    base = os.path.realpath(os.path.join(WORKSPACE_DIR, "users", _safe_user_segment(user_id)))
    if os.path.commonpath([base, WORKSPACE_DIR]) != WORKSPACE_DIR:
        raise SandboxViolation("Invalid user id.")
    return base


def ensure_user_workspace(user_id: str) -> str:
    base = user_workspace_dir(user_id)
    os.makedirs(base, exist_ok=True)
    os.makedirs(os.path.join(base, "sandbox_scripts"), exist_ok=True)
    return base


class SandboxViolation(PermissionError):
    pass


def resolve_safe_path(user_path: str, user_id: str = None) -> str:
    """Resolve user_path relative to the sandbox root and guarantee the
    result is still inside it. Raises SandboxViolation otherwise. This is
    the ONLY function in the codebase allowed to turn a user/LLM-supplied
    path string into a real filesystem path for file_tools or self_healing
    to use.

    `user_id`, when given, confines resolution to that user's own subtree
    (see user_workspace_dir) rather than the shared WORKSPACE_DIR root —
    every multi-user caller must pass it. Its absence is only for
    single-tenant callers (the desktop build has no concept of "users").
    """
    if not user_path or not isinstance(user_path, str):
        raise SandboxViolation("Empty or invalid path.")

    root = user_workspace_dir(user_id) if user_id else WORKSPACE_DIR

    # Treat the incoming path as relative to the workspace even if it looks
    # absolute or tries to climb out with "..". realpath collapses "..".
    candidate = os.path.realpath(os.path.join(root, user_path.lstrip("/\\")))

    if os.path.commonpath([candidate, root]) != root:
        raise SandboxViolation(
            f"'{user_path}' resolves outside the JARVIS workspace. Refused."
        )
    if _touches_denylist(candidate):
        raise SandboxViolation(f"'{user_path}' touches a protected system path. Refused.")

    return candidate


# ---------------------------------------------------------------------------
# 2. No admin / elevated privileges
# ---------------------------------------------------------------------------
def is_elevated() -> bool:
    try:
        if os.name == "nt":
            import ctypes
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        # POSIX fallback, relevant if this is ever run on macOS/Linux for dev.
        return hasattr(os, "geteuid") and os.geteuid() == 0
    except Exception as e:
        logger.warning(f"Could not determine privilege level ({e}); assuming non-elevated.")
        return False


def refuse_if_elevated():
    """Call this once at startup. A screen/keyboard/process-control agent
    running with admin rights can escape any sandbox check above it — the
    sandbox only constrains JARVIS's own code, not what elevated OS APIs
    would allow pyautogui/subprocess to touch. So we simply never run
    elevated, rather than trying to be extra careful while elevated.
    """
    if is_elevated():
        raise SystemExit(
            "\n[J.A.R.V.I.S cannot start]\n"
            "JARVIS refuses to start with administrator/root privileges — this "
            "is a deliberate safety limit, not a bug.\n"
            "Close this window and run it as your normal (non-admin) user.\n"
        )


# ---------------------------------------------------------------------------
# 3. Confirm-to-act registry for destructive actions
# ---------------------------------------------------------------------------
# Anything that lands here does NOT execute yet. It's parked with a token,
# the Flask layer surfaces it to the HUD/voice ("Sir, confirm: delete X?"),
# and only /api/confirm/<token> with approve=True actually runs the callback.
_PENDING = {}
_PENDING_TTL_SECONDS = 300


def request_confirmation(description: str, callback, *cb_args, meta: dict = None, **cb_kwargs) -> str:
    """Register a destructive action. Returns a token to show the user.
    `callback(*cb_args, **cb_kwargs)` runs only on approval.

    `meta` is opaque bookkeeping (e.g. which provider/user this proposal is
    for) that the registering module needs later to rebuild `cb_args` if the
    user edits the proposal before approving — see update_pending() below.
    """
    token = secrets.token_hex(16)  # 128 bits — token_hex(4) (32 bits) was cheaply guessable
    _PENDING[token] = {
        "description": description,
        "callback": callback,
        "args": cb_args,
        "kwargs": cb_kwargs,
        "created": time.time(),
        "meta": meta or {},
    }
    logger.info(f"Confirmation requested [{token}]: {description}")
    return token


def get_pending_meta(token: str) -> dict | None:
    _purge_expired()
    entry = _PENDING.get(token)
    return dict(entry["meta"]) if entry else None


def update_pending(token: str, args: tuple, description: str) -> bool:
    """Swap in edited args/description for a still-pending confirmation.
    Approving afterward re-runs the same callback with these new args
    instead of the ones originally proposed — the callback itself never
    changes, only what it's called with."""
    _purge_expired()
    entry = _PENDING.get(token)
    if entry is None:
        return False
    entry["args"] = args
    entry["description"] = description
    return True


def update_pending_meta(token: str, **meta_updates) -> bool:
    """Merges into a still-pending confirmation's meta — used when editing
    a field that only meta tracks (e.g. a composed email's attachments,
    which the edit form has no way to re-specify) so it survives an edit
    to unrelated fields instead of silently reverting to "none"."""
    _purge_expired()
    entry = _PENDING.get(token)
    if entry is None:
        return False
    entry["meta"].update(meta_updates)
    return True


def _purge_expired():
    now = time.time()
    for tok in [t for t, v in _PENDING.items() if now - v["created"] > _PENDING_TTL_SECONDS]:
        del _PENDING[tok]


def list_pending():
    _purge_expired()
    return {t: v["description"] for t, v in _PENDING.items()}


def resolve_confirmation(token: str, approve: bool) -> str:
    _purge_expired()
    entry = _PENDING.pop(token, None)
    if entry is None:
        return "That confirmation has expired or doesn't exist, sir."
    if not approve:
        return f"Cancelled: {entry['description']}"
    try:
        result = entry["callback"](*entry["args"], **entry["kwargs"])
        return result if isinstance(result, str) else f"Done: {entry['description']}"
    except Exception as e:
        # Full detail goes to the log; the spoken reply stays clean — this
        # is the one place a raw provider error (an HttpError repr, an
        # expired-token stack trace, etc.) would otherwise get read aloud
        # verbatim right after the user approved the action.
        logger.error(f"Confirmed action failed: {e}")
        return f"That action failed after you approved it, sir — {entry['description']} didn't go through. Please try again."


# Processes that must never be killed even with confirmation — the confirm
# step is for "are you sure about YOUR app", not "let's take down the OS".
CRITICAL_PROCESS_DENYLIST = {
    "system", "system idle process", "csrss.exe", "wininit.exe", "winlogon.exe",
    "services.exe", "lsass.exe", "smss.exe", "explorer.exe", "svchost.exe",
    "registry", "dwm.exe",
}
