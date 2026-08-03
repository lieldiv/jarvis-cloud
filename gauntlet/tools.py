"""
Gauntlet tool layer.

This does NOT reimplement weather/open-app/Spotify/Paint/calculator — it
imports the already-hardened versions straight from app.py so there is only
one copy of that logic to maintain and secure. This module only adds what
app.py didn't need: a general "run a command" tool for the agent loop, plus
the JSON-schema tool list and dispatch table the Builder/Critic agents use.

IMPORTANT SECURITY NOTE ON run_shell_command:
  This takes an argv LIST (["ping", "-n", "1", "8.8.8.8"]), never a raw
  shell string, and is always invoked with shell=False. That means shell
  metacharacters (&, |, ;, `, $(...)) are inert - they're passed as literal
  argv text to the child process, not interpreted by a shell. This is the
  same protection app.py already applies to open_application. It does NOT
  make arbitrary code execution "safe" in an absolute sense: the agent can
  still run any program on PATH. Treat this the way you'd treat any local
  automation script with access to your shell - it runs with your user's
  permissions. ALLOWED_COMMANDS below is an allowlist of executable names;
  extend it deliberately, don't disable it.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger("gauntlet.tools")

# Make the existing app.py importable regardless of cwd.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app as jarvis  # noqa: E402  (existing, hardened tool implementations)

# ---------------------------------------------------------------------------
# Allowlisted executables for the generic command-runner tool. This is the
# one new capability beyond what app.py already exposed (opening named apps,
# Spotify, Paint, calculator). Add entries only for programs you actually
# want the agent invoking; the Builder cannot bypass this list.
# ---------------------------------------------------------------------------
ALLOWED_COMMANDS = {
    "dir", "where", "whoami", "echo", "ping", "tasklist", "systeminfo",
    "ipconfig", "python", "pip",
}

COMMAND_TIMEOUT_SECONDS = 20


def run_shell_command(argv: list[str]) -> str:
    """Execute an allowlisted program with explicit arguments (no shell).

    argv[0] must be one of ALLOWED_COMMANDS. Everything else in argv is
    passed verbatim as separate arguments - never concatenated into a
    string a shell would re-parse.
    """
    if not argv or not isinstance(argv, list):
        return "No command was given."

    program = str(argv[0]).lower()
    if program not in ALLOWED_COMMANDS:
        return (
            f"'{argv[0]}' is not on the allowlist, so I won't run it. "
            f"Allowed: {', '.join(sorted(ALLOWED_COMMANDS))}."
        )

    try:
        result = subprocess.run(
            [str(a) for a in argv],
            shell=False,
            capture_output=True,
            text=True,
            timeout=COMMAND_TIMEOUT_SECONDS,
        )
        output = (result.stdout or "").strip()
        error = (result.stderr or "").strip()
        summary = output if output else "(no stdout)"
        if error:
            summary += f"\n[stderr] {error}"
        if result.returncode != 0:
            summary += f"\n[exit code {result.returncode}]"
        return summary[:2000]  # keep observations bounded for the Critic
    except subprocess.TimeoutExpired:
        return f"Command timed out after {COMMAND_TIMEOUT_SECONDS}s."
    except Exception as e:
        logger.error(f"run_shell_command failed: {e}")
        return f"Command failed to execute: {e}"


# ---------------------------------------------------------------------------
# Tool schema + dispatch, reusing app.py's implementations directly
# ---------------------------------------------------------------------------
TOOLS = jarvis.TOOLS + [
    {
        "type": "function",
        "function": {
            "name": "run_shell_command",
            "description": (
                "Run an allowlisted local command for inspection/diagnostics "
                "(e.g. dir, whoami, ping, tasklist, ipconfig, python, pip). "
                "Pass argv as a list of separate arguments, never a single "
                "shell string."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "argv": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Program name followed by its arguments, e.g. [\"ping\", \"-n\", \"1\", \"8.8.8.8\"]",
                    }
                },
                "required": ["argv"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "task_complete",
            "description": (
                "Call this when the user's goal has been fully achieved and "
                "no further tool calls are needed. Provide a short summary."
            ),
            "parameters": {
                "type": "object",
                "properties": {"summary": {"type": "string"}},
                "required": ["summary"],
            },
        },
    },
]

TOOL_IMPL = dict(jarvis.TOOL_IMPL)
TOOL_IMPL["run_shell_command"] = lambda args: run_shell_command(args.get("argv", []))
TOOL_IMPL["task_complete"] = lambda args: args.get("summary", "Task marked complete.")


def execute_tool_call(name: str, arguments_json: str) -> str:
    """Parse a tool call's JSON arguments and dispatch it. Never raises -
    the Gauntlet loop treats a returned error string as a normal
    observation for the Critic to react to, rather than crashing the run."""
    try:
        args = json.loads(arguments_json or "{}")
    except json.JSONDecodeError:
        args = {}

    impl = TOOL_IMPL.get(name)
    if not impl:
        return f"Unknown tool '{name}'."

    try:
        return impl(args)
    except Exception as e:
        logger.error(f"Tool '{name}' raised: {e}")
        return f"Tool '{name}' raised an error: {e}"
