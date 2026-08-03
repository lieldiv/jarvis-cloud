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


class SandboxViolation(PermissionError):
    pass


def resolve_safe_path(user_path: str) -> str:
    """Resolve user_path relative to WORKSPACE_DIR and guarantee the result
    is still inside it. Raises SandboxViolation otherwise. This is the ONLY
    function in the codebase allowed to turn a user/LLM-supplied path string
    into a real filesystem path for file_tools or self_healing to use.
    """
    if not user_path or not isinstance(user_path, str):
        raise SandboxViolation("Empty or invalid path.")

    # Treat the incoming path as relative to the workspace even if it looks
    # absolute or tries to climb out with "..". realpath collapses "..".
    candidate = os.path.realpath(os.path.join(WORKSPACE_DIR, user_path.lstrip("/\\")))

    if os.path.commonpath([candidate, WORKSPACE_DIR]) != WORKSPACE_DIR:
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


def request_confirmation(description: str, callback, *cb_args, **cb_kwargs) -> str:
    """Register a destructive action. Returns a token to show the user.
    `callback(*cb_args, **cb_kwargs)` runs only on approval.
    """
    token = secrets.token_hex(4)
    _PENDING[token] = {
        "description": description,
        "callback": callback,
        "args": cb_args,
        "kwargs": cb_kwargs,
        "created": time.time(),
    }
    logger.info(f"Confirmation requested [{token}]: {description}")
    return token


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
