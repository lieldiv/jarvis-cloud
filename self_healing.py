"""
self_healing.py — autonomous self-testing/self-correction loop.

This is what stands between "JARVIS writes code" and "you become the QA
department". Given a goal and a code-generation function, it:
  1. runs the current candidate script in a sandboxed subprocess,
  2. captures stdout/stderr/return code (no exceptions escape to the caller),
  3. if it failed, hands the traceback + code back to the LLM and asks for a
     fixed version,
  4. repeats up to MAX_ITERATIONS, returning the first success or a final
     failure report — either way, one clean answer for the user, not a
     stream of tracebacks to copy-paste.

Sandboxing notes:
  - Scripts are written to WORKSPACE/sandbox_scripts and run with
    cwd=WORKSPACE, so relative file access from generated code stays
    confined the same way file_tools.py confines it.
  - subprocess.run with a hard timeout — a generated infinite loop can't
    hang the assistant forever.
  - This restricts *where* code runs and *for how long*, not *what syscalls*
    it can make (that would need an OS-level sandbox/container, which is a
    good next step if you start letting it write more ambitious scripts).
    Treat it as a blast-radius limiter, not a security boundary against
    hostile code — it doesn't need to be one, since JARVIS is generating the
    code from your own goal, not running untrusted third-party input.
"""

import logging
import os
import subprocess
import sys
import time
import uuid

from guardrails import resolve_safe_path, ensure_workspace, ensure_user_workspace, WORKSPACE_DIR

logger = logging.getLogger("jarvis.self_healing")

MAX_ITERATIONS = 5
EXEC_TIMEOUT_SECONDS = 15

FIX_PROMPT_TEMPLATE = """\
The following Python script was run and failed. Fix it. Respond with ONLY the \
complete corrected Python source code — no markdown fences, no explanation.

GOAL:
{goal}

PREVIOUS CODE:
{code}

STDOUT:
{stdout}

STDERR / TRACEBACK:
{stderr}
"""


def run_in_sandbox(code: str, user_id: str = None) -> dict:
    """Write `code` to a fresh file under sandbox_scripts and execute it.
    Returns {"success": bool, "stdout": str, "stderr": str, "returncode": int}.

    `user_id`, when given, confines this to that user's own subtree of the
    workspace (see guardrails.user_workspace_dir) — without it, every
    signed-in user's self-healing attempts would land in the same shared
    sandbox_scripts folder, each able to see/overwrite the others'.
    """
    if user_id:
        cwd = ensure_user_workspace(user_id)
    else:
        ensure_workspace()
        cwd = WORKSPACE_DIR
    fname = f"attempt_{uuid.uuid4().hex[:8]}.py"
    script_path = resolve_safe_path(os.path.join("sandbox_scripts", fname), user_id=user_id)

    with open(script_path, "w", encoding="utf-8") as f:
        f.write(code)

    try:
        proc = subprocess.run(
            [sys.executable, script_path],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=EXEC_TIMEOUT_SECONDS,
        )
        return {
            "success": proc.returncode == 0,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
            "returncode": proc.returncode,
        }
    except subprocess.TimeoutExpired as e:
        return {
            "success": False,
            "stdout": e.stdout or "",
            "stderr": f"Execution timed out after {EXEC_TIMEOUT_SECONDS}s (possible infinite loop).",
            "returncode": -1,
        }


def _ask_llm_to_fix(groq_client, model_name: str, goal: str, code: str, stdout: str, stderr: str) -> str:
    prompt = FIX_PROMPT_TEMPLATE.format(goal=goal, code=code, stdout=stdout[-2000:], stderr=stderr[-2000:])
    completion = groq_client.chat.completions.create(
        model=model_name,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.2,
        max_tokens=1200,
    )
    text = (completion.choices[0].message.content or "").strip()
    return text.removeprefix("```python").removeprefix("```").removesuffix("```").strip()


def self_healing_code_loop(groq_client, model_name: str, goal: str, initial_code: str, on_attempt=None, user_id: str = None) -> dict:
    """Run `initial_code`, and if it fails, iteratively ask the LLM to fix it
    and re-run, up to MAX_ITERATIONS. `on_attempt(info)` is an optional
    callback for streaming progress into the HUD log.

    Returns {"success": bool, "code": <final code>, "attempts": int,
             "stdout": str, "stderr": str}.
    """
    code = initial_code
    result = None

    for attempt in range(1, MAX_ITERATIONS + 1):
        result = run_in_sandbox(code, user_id=user_id)
        if on_attempt:
            on_attempt({
                "attempt": attempt,
                "success": result["success"],
                "stderr_snippet": result["stderr"][:300],
            })

        if result["success"]:
            return {"success": True, "code": code, "attempts": attempt,
                     "stdout": result["stdout"], "stderr": result["stderr"]}

        if attempt == MAX_ITERATIONS:
            break

        logger.info(f"Self-heal attempt {attempt} failed, asking model for a fix.")
        try:
            code = _ask_llm_to_fix(groq_client, model_name, goal, code, result["stdout"], result["stderr"])
        except Exception as e:
            logger.error(f"Fix-generation call failed: {e}")
            break
        time.sleep(0.2)  # small backoff between attempts

    return {"success": False, "code": code, "attempts": MAX_ITERATIONS,
             "stdout": result["stdout"] if result else "", "stderr": result["stderr"] if result else ""}
