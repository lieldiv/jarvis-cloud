"""
file_tools.py — file system and process-control tools, all routed through
guardrails.py. Read/write/list act immediately (confined to the workspace,
nothing destructive). Delete and process-kill register a confirmation
instead of acting, per the "critical actions need a human" requirement.
"""

import logging
import os
import shutil

import psutil  # add to requirements.txt: psutil

from guardrails import (
    resolve_safe_path, ensure_workspace, ensure_user_workspace, SandboxViolation,
    request_confirmation, CRITICAL_PROCESS_DENYLIST,
)

logger = logging.getLogger("jarvis.file_tools")


def list_dir(rel_path: str = ".", user_id: str = None) -> str:
    ensure_user_workspace(user_id) if user_id else ensure_workspace()
    try:
        path = resolve_safe_path(rel_path, user_id=user_id)
    except SandboxViolation as e:
        return str(e)
    if not os.path.isdir(path):
        return f"'{rel_path}' is not a directory in the workspace, sir."
    entries = sorted(os.listdir(path))
    return ", ".join(entries) if entries else "(empty)"


def read_file(rel_path: str, max_chars: int = 4000, user_id: str = None) -> str:
    try:
        path = resolve_safe_path(rel_path, user_id=user_id)
    except SandboxViolation as e:
        return str(e)
    if not os.path.isfile(path):
        return f"'{rel_path}' doesn't exist in the workspace, sir."
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        content = f.read(max_chars)
    return content


def write_file(rel_path: str, content: str, user_id: str = None) -> str:
    ensure_user_workspace(user_id) if user_id else ensure_workspace()
    try:
        path = resolve_safe_path(rel_path, user_id=user_id)
    except SandboxViolation as e:
        return str(e)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return f"Wrote {len(content)} characters to {rel_path}, sir."


def _do_delete(path: str) -> str:
    try:
        if os.path.isdir(path):
            shutil.rmtree(path)
        else:
            os.remove(path)
        return "Deleted, sir."
    except FileNotFoundError:
        return "Already gone, sir."
    except PermissionError as e:
        logger.error(f"Delete failed for {path} (permission denied): {e}")
        return "I don't have permission to delete that, sir — it may be open in another program."
    except Exception as e:
        logger.error(f"Delete failed for {path}: {e}")
        return "That delete failed, sir — please check the file and try again."


def delete_path(rel_path: str, user_id: str = None) -> dict:
    """Never deletes directly. Registers a confirmation and returns the
    token + description for the caller (Flask route) to surface to the HUD.
    Paths outside the workspace are refused outright — no confirmation can
    override that, by design. The resolved (already user-scoped) path is
    what gets captured in the confirmation callback, so approval always
    acts on this user's own file regardless of who clicks approve.
    """
    try:
        path = resolve_safe_path(rel_path, user_id=user_id)
    except SandboxViolation as e:
        return {"status": "refused", "message": str(e)}

    if not os.path.exists(path):
        return {"status": "refused", "message": f"'{rel_path}' doesn't exist, sir."}

    description = f"Delete '{rel_path}' from the JARVIS workspace"
    token = request_confirmation(description, _do_delete, path, meta={"kind": "file_delete", "user_id": user_id})
    return {"status": "confirmation_required", "token": token, "message": description}


def _do_kill(pid: int) -> str:
    try:
        proc = psutil.Process(pid)
        name = proc.name()
        proc.terminate()
        return f"Terminated {name} (pid {pid}), sir."
    except psutil.NoSuchProcess:
        return "That process is no longer running, sir."
    except psutil.AccessDenied:
        logger.error(f"Kill failed for pid {pid} (access denied)")
        return "I don't have permission to terminate that process, sir."
    except Exception as e:
        logger.error(f"Kill failed for pid {pid}: {e}")
        return "I couldn't terminate that process, sir — please try again."


def kill_process(pid: int) -> dict:
    """Always requires confirmation. Refuses outright — no confirmation
    possible — for anything on the critical-process denylist, since that's
    a system-stability risk rather than a 'did you mean it' question.
    """
    try:
        proc = psutil.Process(pid)
        name = proc.name()
    except psutil.NoSuchProcess:
        return {"status": "refused", "message": "No process with that ID, sir."}
    except Exception as e:
        logger.warning(f"Couldn't inspect pid {pid}: {e}")
        return {"status": "refused", "message": "I couldn't inspect that process, sir."}

    if name.lower() in CRITICAL_PROCESS_DENYLIST:
        return {"status": "refused", "message": f"Refusing to touch a critical system process ({name}), sir."}

    description = f"Terminate process '{name}' (pid {pid})"
    token = request_confirmation(description, _do_kill, pid)
    return {"status": "confirmation_required", "token": token, "message": description}
