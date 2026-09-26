"""In-memory fakes for every port, so the loop runs end to end before the real pieces exist.

They are deterministic and record their calls, which is what the controller tests assert on.
Each one passes the same contract tests (tests/contracts/) the real implementations must pass.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from tokeneyezed.ports import (
    AttemptKilled,
    AttemptResult,
    Goal,
    Ports,
    Review,
    Score,
)


@dataclass
class InMemoryGoalStore:
    goals: dict[str, dict[str, Any]] = field(default_factory=dict)
    replans: list[tuple[str, str]] = field(default_factory=list)

    def seed(self, session_id: str, sections: Sequence[str], target_val_pass: float) -> None:
        for priority, section in enumerate(sections):
            goal_id = f"{session_id}:{section}"
            self.goals[goal_id] = {
                "session_id": session_id,
                "section": section,
                "target": target_val_pass,
                "status": "open",
                "priority": priority,
            }

    def next_open(self, session_id: str) -> Goal | None:
        open_goals = [
            (g["priority"], goal_id, g)
            for goal_id, g in self.goals.items()
            if g["session_id"] == session_id and g["status"] == "open"
        ]
        if not open_goals:
            return None
        _, goal_id, g = min(open_goals)
        return Goal(goal_id=goal_id, section=g["section"], target_val_pass=g["target"])

    def replan(self, goal_id: str, note: str) -> None:
        self.replans.append((goal_id, note))

    def complete(self, goal_id: str) -> None:
        self.goals[goal_id]["status"] = "complete"


@dataclass
class FakeBriefBuilder:
    calls: list[tuple[str, bool]] = field(default_factory=list)

    def build(self, session_id: str, goal: Goal, use_memory: bool) -> str:
        self.calls.append((goal.goal_id, use_memory))
        return f"Work on {goal.section}. Memory {'on' if use_memory else 'off'}."


class FakePlanner:
    def plan(self, goal: Goal, brief: str) -> str:
        return f"Improve {goal.section}"


@dataclass
class FakeRunner:
    agent: str = "claude"
    kill_on_call: int | None = None  # simulate SIGTERM during this call (1-based)
    calls: int = 0
    resets: list[str | None] = field(default_factory=list)

    def run(self, *, session_id: str, attempt_id: str, brief: str, intent: str) -> AttemptResult:
        self.calls += 1
        if self.calls == self.kill_on_call:
            raise AttemptKilled(f"{self.agent} terminated during {attempt_id}")
        return AttemptResult(
            agent=self.agent,
            commit=f"{self.agent}-{attempt_id}",
            diff_summary=f"{intent} ({self.agent})",
            exit_code=0,
        )

    def reset_workspace(self, commit: str | None) -> None:
        self.resets.append(commit)


@dataclass
class ScriptedScorer:
    """Returns the next validation pass rate from a script (the last value repeats)."""

    val_script: Sequence[float] = (1.0,)
    calls: int = 0

    def score(self) -> Score:
        val = self.val_script[min(self.calls, len(self.val_script) - 1)]
        self.calls += 1
        visible = min(1.0, val + 0.05)
        return Score(visible_pass=visible, val_pass=val, per_section={})


@dataclass
class FakeReviewer:
    flag_calls: frozenset[int] = frozenset()  # flag the review on these calls (1-based)
    calls: int = 0

    def review(self, result: AttemptResult, score: Score, previous: Score | None) -> Review:
        self.calls += 1
        if self.calls in self.flag_calls:
            return Review(flagged=True, reasons=("visible rose while validation stayed flat",))
        return Review(flagged=False)


@dataclass
class InMemoryLedger:
    attempts: dict[str, dict[str, Any]] = field(default_factory=dict)

    def open_attempt(
        self,
        *,
        session_id: str,
        attempt_id: str,
        number: int,
        goal_id: str,
        agent: str,
        intent: str,
        parent_attempt: str | None,
    ) -> None:
        self.attempts[attempt_id] = {
            "attempt_id": attempt_id,
            "session_id": session_id,
            "number": number,
            "goal_id": goal_id,
            "agent": agent,
            "intent": intent,
            "parent_attempt": parent_attempt,
            "status": "running",
        }

    def close_attempt(
        self,
        *,
        attempt_id: str,
        result: AttemptResult,
        score: Score,
        outcome: str,
        observer_flags: Sequence[str],
    ) -> None:
        self.attempts[attempt_id].update(
            status="closed",
            commit=result.commit,
            val_pass=score.val_pass,
            outcome=outcome,
            observer_flags=list(observer_flags),
        )

    def mark_running_as_killed(self, session_id: str) -> list[str]:
        killed = [
            attempt_id
            for attempt_id, a in self.attempts.items()
            if a["session_id"] == session_id and a["status"] == "running"
        ]
        for attempt_id in killed:
            self.attempts[attempt_id]["status"] = "killed"
        return killed

    def last_clean_commit(self, session_id: str) -> str | None:
        clean = [
            a
            for a in self.attempts.values()
            if a["session_id"] == session_id
            and a["status"] == "closed"
            and a["outcome"] != "flagged"
        ]
        return max(clean, key=lambda a: a["number"])["commit"] if clean else None

    def closed(self, session_id: str) -> list[dict[str, Any]]:
        return sorted(
            (
                a
                for a in self.attempts.values()
                if a["session_id"] == session_id and a["status"] == "closed"
            ),
            key=lambda a: a["number"],
        )


@dataclass
class FakeCompactor:
    calls: list[tuple[str, str]] = field(default_factory=list)

    def compact(self, session_id: str, goal_id: str) -> None:
        self.calls.append((session_id, goal_id))


def fake_ports(agent: str = "claude", **overrides: Any) -> Ports:
    defaults: dict[str, Any] = {
        "goals": InMemoryGoalStore(),
        "brief": FakeBriefBuilder(),
        "planner": FakePlanner(),
        "runner": FakeRunner(agent=agent),
        "scorer": ScriptedScorer(),
        "reviewer": FakeReviewer(),
        "ledger": InMemoryLedger(),
        "compactor": FakeCompactor(),
    }
    return Ports(**{**defaults, **overrides})
