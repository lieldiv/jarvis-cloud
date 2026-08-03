"""
Builder and Critic agents for the Gauntlet Loop.

Builder: proposes exactly one next tool call toward the goal, given the
         history of actions/observations/critic feedback so far.
Critic:  looks at what the Builder just did and its observed result, and
         decides PASS (goal achieved), CONTINUE (fine, keep going), or
         RETRY (that step failed / was wrong - here's feedback for the
         Builder's next attempt).

Both are just Groq chat-completion calls with different system prompts;
the Builder additionally gets tool-calling so it can act, while the Critic
is a pure text judge (it never calls tools itself, so it can't paper over
a bad action by "fixing" it silently).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from . import tools as gtools
from .config import get_groq_client, MODEL_NAME

logger = logging.getLogger("gauntlet.agents")

BUILDER_SYSTEM_PROMPT = (
    "You are the Builder in an autonomous desktop-automation loop running on "
    "the user's own Windows machine. You are given a GOAL and a transcript of "
    "actions taken so far, their observed results, and any Critic feedback. "
    "On each turn, call exactly ONE tool that makes the most progress toward "
    "the goal. If the goal is already fully achieved, call task_complete with "
    "a short summary instead of any other tool. Never call more than one tool "
    "per turn. If Critic feedback says a previous step failed, do not repeat "
    "the identical action - adjust your approach."
)

CRITIC_SYSTEM_PROMPT = (
    "You are the Critic in an autonomous desktop-automation loop. You review "
    "one Builder action and its observed result against the stated GOAL. "
    "Respond with ONLY a JSON object, no other text, of the form: "
    '{"verdict": "PASS" | "CONTINUE" | "RETRY", "feedback": "<short reason>"}. '
    "Use PASS only when the goal is fully and verifiably achieved. Use RETRY "
    "when the action failed, produced an error, or moved away from the goal - "
    "feedback should say concretely what to try differently. Use CONTINUE "
    "when the step was a reasonable partial step and more actions are still "
    "needed. Be skeptical: a tool call succeeding is not the same as the "
    "user's goal being met."
)


@dataclass
class StepRecord:
    tool_name: str
    tool_args: dict
    observation: str
    critic_verdict: str
    critic_feedback: str


@dataclass
class GauntletTranscript:
    goal: str
    steps: list[StepRecord] = field(default_factory=list)

    def as_prompt_text(self) -> str:
        if not self.steps:
            return "(no actions taken yet)"
        lines = []
        for i, s in enumerate(self.steps, 1):
            lines.append(
                f"Step {i}: called {s.tool_name}({json.dumps(s.tool_args)})\n"
                f"  observation: {s.observation}\n"
                f"  critic: {s.critic_verdict} - {s.critic_feedback}"
            )
        return "\n".join(lines)


class BuilderAgent:
    def __init__(self):
        self.client = get_groq_client()

    def propose_action(self, transcript: GauntletTranscript):
        """Returns (tool_name, tool_args_dict, raw_text_or_None).

        raw_text_or_None is set instead of a tool call only if the model
        refused to call any tool at all (rare, but handled defensively).
        """
        messages = [
            {"role": "system", "content": BUILDER_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"GOAL: {transcript.goal}\n\n"
                    f"TRANSCRIPT SO FAR:\n{transcript.as_prompt_text()}\n\n"
                    "Propose the single next tool call."
                ),
            },
        ]

        completion = self.client.chat.completions.create(
            model=MODEL_NAME,
            messages=messages,
            tools=gtools.TOOLS,
            tool_choice="required",
            temperature=0.3,
            max_tokens=300,
        )
        msg = completion.choices[0].message

        if not msg.tool_calls:
            return None, None, msg.content

        call = msg.tool_calls[0]
        try:
            args = json.loads(call.function.arguments or "{}")
        except json.JSONDecodeError:
            args = {}
        return call.function.name, args, None


class CriticAgent:
    def __init__(self):
        self.client = get_groq_client()

    def review(self, goal: str, tool_name: str, tool_args: dict, observation: str):
        """Returns (verdict, feedback) where verdict in {PASS, CONTINUE, RETRY}."""
        messages = [
            {"role": "system", "content": CRITIC_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"GOAL: {goal}\n"
                    f"ACTION: {tool_name}({json.dumps(tool_args)})\n"
                    f"OBSERVATION: {observation}\n\n"
                    "Return the JSON verdict now."
                ),
            },
        ]

        try:
            completion = self.client.chat.completions.create(
                model=MODEL_NAME,
                messages=messages,
                temperature=0.0,
                max_tokens=150,
            )
            raw = completion.choices[0].message.content or "{}"
            # Models sometimes wrap JSON in ```json fences despite instructions.
            raw = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
            parsed = json.loads(raw)
            verdict = str(parsed.get("verdict", "CONTINUE")).upper()
            if verdict not in ("PASS", "CONTINUE", "RETRY"):
                verdict = "CONTINUE"
            feedback = str(parsed.get("feedback", ""))
            return verdict, feedback
        except Exception as e:
            logger.warning(f"Critic parse failure, defaulting to CONTINUE: {e}")
            return "CONTINUE", f"(critic response unparseable: {e})"
