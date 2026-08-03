"""
The Gauntlet Loop.

    for round in range(max_rounds):
        action = Builder.propose_action(transcript)
        if action is task_complete: stop, report success
        observation = execute(action)
        verdict, feedback = Critic.review(goal, action, observation)
        record step; if verdict == PASS: stop, report success
        # otherwise (CONTINUE or RETRY) loop again - the Builder sees the
        # Critic's feedback in the transcript on its next turn either way.

This is intentionally a *loop*, not a single pass: RETRY doesn't raise an
exception or ask a human, it just becomes part of the transcript the
Builder reasons over next turn, so the agent can self-correct within the
same run. A human is not in this loop by design (that's the point of an
autonomous agent) - the safety boundary instead lives in the tool layer
(gauntlet/tools.py's allowlist) and in max_rounds capping runaway loops.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Optional

from . import tools as gtools
from .agents import BuilderAgent, CriticAgent, GauntletTranscript, StepRecord
from .config import MAX_GAUNTLET_ROUNDS

logger = logging.getLogger("gauntlet.loop")

# Event callback signature: on_event(event_type: str, payload: dict)
EventCallback = Callable[[str, dict], None]


@dataclass
class GauntletResult:
    success: bool
    rounds_used: int
    summary: str
    transcript: GauntletTranscript


class GauntletLoop:
    def __init__(self, max_rounds: int = MAX_GAUNTLET_ROUNDS):
        self.max_rounds = max_rounds
        self.builder = BuilderAgent()
        self.critic = CriticAgent()
        self._stop_requested = False

    def request_stop(self):
        """Called from another thread (e.g. the GUI's Stop button) to end
        the loop after the current round finishes."""
        self._stop_requested = True

    def run(self, goal: str, on_event: Optional[EventCallback] = None) -> GauntletResult:
        def emit(event_type: str, **payload):
            if on_event:
                try:
                    on_event(event_type, payload)
                except Exception:
                    logger.exception("on_event callback raised")

        self._stop_requested = False
        transcript = GauntletTranscript(goal=goal)
        emit("start", goal=goal, max_rounds=self.max_rounds)

        for round_num in range(1, self.max_rounds + 1):
            if self._stop_requested:
                emit("stopped", round=round_num)
                return GauntletResult(False, round_num, "Stopped by user.", transcript)

            emit("round_start", round=round_num)

            try:
                tool_name, tool_args, refusal_text = self.builder.propose_action(transcript)
            except Exception as e:
                logger.error(f"Builder failed: {e}")
                emit("error", stage="builder", error=str(e))
                return GauntletResult(False, round_num, f"Builder error: {e}", transcript)

            if tool_name is None:
                # Model returned prose instead of a tool call - treat it as
                # a completion summary rather than looping forever.
                emit("builder_refused", text=refusal_text)
                return GauntletResult(True, round_num, refusal_text or "Done.", transcript)

            emit("action", tool=tool_name, args=tool_args)

            if tool_name == "task_complete":
                summary = tool_args.get("summary", "Task marked complete.")
                emit("complete", summary=summary)
                return GauntletResult(True, round_num, summary, transcript)

            observation = gtools.execute_tool_call(
                tool_name, __import__("json").dumps(tool_args)
            )
            emit("observation", tool=tool_name, observation=observation)

            try:
                verdict, feedback = self.critic.review(goal, tool_name, tool_args, observation)
            except Exception as e:
                logger.error(f"Critic failed: {e}")
                verdict, feedback = "CONTINUE", f"(critic error: {e})"

            emit("critic", verdict=verdict, feedback=feedback)

            transcript.steps.append(
                StepRecord(
                    tool_name=tool_name,
                    tool_args=tool_args,
                    observation=observation,
                    critic_verdict=verdict,
                    critic_feedback=feedback,
                )
            )

            if verdict == "PASS":
                emit("complete", summary=feedback or "Goal achieved.")
                return GauntletResult(True, round_num, feedback or "Goal achieved.", transcript)

        emit("exhausted", rounds=self.max_rounds)
        return GauntletResult(
            False,
            self.max_rounds,
            f"Gave up after {self.max_rounds} rounds without the Critic confirming success.",
            transcript,
        )
