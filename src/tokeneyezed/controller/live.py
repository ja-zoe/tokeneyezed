"""Live feed: the harness as the judges see it during the demo.

One short block per attempt, and loud markers for the moments the demo points at (flag, replan,
goal complete, kill, resume). Everything else stays quiet, so a pane left running all afternoon
still reads at a glance.
"""

from __future__ import annotations

import os
import sys
from typing import Any, TextIO

STYLES = {"bold": "1", "red": "1;31", "green": "1;32", "yellow": "1;33", "cyan": "1;36"}


class LiveFeed:
    def __init__(
        self, agent: str = "", out: TextIO | None = None, color: bool | None = None
    ) -> None:
        self.agent = agent
        self.out = out or sys.stdout  # resolved per instance, so redirects are honored
        if color is None:
            color = self.out.isatty() and not os.environ.get("NO_COLOR")
        self.color = color
        self.goal_id = ""
        self.section = "-"
        self.done = 0  # finished attempts; the one in flight is done + 1
        self.best: dict[str, float] = {}
        self.streaks: dict[str, int] = {}

    def _style(self, text: str, style: str) -> str:
        return f"\033[{STYLES[style]}m{text}\033[0m" if self.color else text

    def line(self, text: str = "") -> None:
        print(text, file=self.out, flush=True)

    def banner(self, text: str, style: str = "bold") -> None:
        self.line(self._style(f"== {text}", style))

    def resumed(
        self,
        session_id: str,
        agent: str,
        killed: list[str],
        state: dict[str, Any],
        next_nodes: tuple[str, ...] = (),
    ) -> None:
        """Banner for a resume, and seed the feed from the checkpoint it continues from."""
        self.agent = agent
        self.done = state.get("attempt_count", 0)
        self.best = dict(state.get("best_val", {}))
        self.streaks = dict(state.get("streaks", {}))
        if state.get("goal"):
            self.goal_id, self.section = state["goal"]["goal_id"], state["goal"]["section"]
        self.line()
        self.banner(f"RESUMED {session_id} from its Atlas checkpoint", "green")
        self.line(f"   agent now      {agent}")
        self.line(f"   attempts done  {state.get('attempt_count', 0)}")
        self.line(f"   killed         {', '.join(killed) or 'none'} (recorded, never scored)")
        for goal_id, best in sorted(self.best.items()):
            self.line(f"   best val {best:.2f}  {goal_id.split(':', 1)[-1]}")
        if "run_attempt" in next_nodes:  # killed mid-attempt: it restarts with the new agent
            self.on_plan_attempt({"intent": state["intent"]})

    def __call__(self, node: str, update: dict[str, Any]) -> None:
        handler = getattr(self, f"on_{node}", None)
        if handler:
            handler(update)

    def on_pick_goal(self, update: dict[str, Any]) -> None:
        if update.get("goal"):
            self.goal_id = update["goal"]["goal_id"]
            self.section = update["goal"]["section"]

    def on_plan_attempt(self, update: dict[str, Any]) -> None:
        # Printed when the attempt starts: a real attempt runs for minutes.
        self.line()
        self.line(self._style(f"#{self.done + 1:03d}  {self.agent:<7} {self.section}", "cyan"))
        self.line(f"      intent  {update['intent']}")

    def on_run_attempt(self, update: dict[str, Any]) -> None:
        self.blocked(update["result"].get("blocked", ()))

    def blocked(self, calls: Any) -> None:
        for call in calls:
            self.line("      " + self._style("BLOCKED", "red") + f"  {call}")

    def on_score_attempt(self, update: dict[str, Any]) -> None:
        score = update["score"]
        self.line(f"      score   visible {score['visible_pass']:.2f}  val {score['val_pass']:.2f}")

    def on_record_flagged(self, update: dict[str, Any]) -> None:
        self.done = update["attempt_count"]
        self.line("      " + self._style("FLAGGED", "red") + "  kept out of memory and metrics")

    def on_review_attempt(self, update: dict[str, Any]) -> None:
        for reason in update.get("flags", []):
            self.line(f"      review  {reason}")

    def on_record_attempt(self, update: dict[str, Any]) -> None:
        self.done = update["attempt_count"]
        new_best = update["best_val"][self.goal_id]
        old_best = self.best.get(self.goal_id)
        self.best[self.goal_id] = new_best
        self.streaks[self.goal_id] = update["streaks"][self.goal_id]
        if old_best is None:
            self.line("      " + self._style(f"first score  {new_best:.2f}", "green"))
        elif new_best > old_best:
            self.line(
                "      " + self._style(f"improved  best {old_best:.2f} -> {new_best:.2f}", "green")
            )
        else:
            streak = self.streaks[self.goal_id]
            self.line(f"      no improvement  (best {new_best:.2f}, {streak} in a row)")

    def on_replan(self, update: dict[str, Any]) -> None:
        self.line("      " + self._style(f"REPLAN  {self.section}: strategy updated", "yellow"))

    def on_complete_goal(self, update: dict[str, Any]) -> None:
        best = self.best.get(self.goal_id, 0.0)
        self.line("      " + self._style(f"GOAL COMPLETE  {self.section} ({best:.2f})", "green"))
