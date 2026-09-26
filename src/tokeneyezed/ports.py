"""Ports: the seam between the controller and the other workstreams.

Graph nodes call these interfaces and never import another workstream's code. Each port has a
fake in `controller/fakes.py`, so the loop runs end to end before the real implementations exist,
and a contract test in `tests/contracts/` that every implementation (fake or real) must pass.

Owners implement the real versions: GoalStore, BriefBuilder, Ledger, Compactor (Aaron); Reviewer
(Dharshan); Scorer (Gunjan); Planner, AttemptRunner (Julian). Changing a signature here is a
contract change: tell the owners first (AGENTS.md).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class Goal:
    goal_id: str
    section: str
    target_val_pass: float  # goal completes when the section's validation pass rate reaches this
    strategy_notes: str = ""  # the current strategy, set by the latest replan


@dataclass(frozen=True)
class AttemptResult:
    agent: str  # "claude", "codex", ...; recorded on the attempt
    commit: str  # commit in the task workspace after the attempt
    diff_summary: str
    exit_code: int
    blocked: tuple[str, ...] = ()  # tool calls the observer's pre-gate blocked, with reasons


@dataclass(frozen=True)
class Score:
    visible_pass: float
    val_pass: float
    # section -> {"visible": x, "val": y}; "visible" is absent for sections too small to have
    # visible examples (validation always has at least one)
    per_section: Mapping[str, Mapping[str, float]]

    def section_val(self, section: str) -> float:
        return self.per_section.get(section, {}).get("val", self.val_pass)


@dataclass(frozen=True)
class Review:
    flagged: bool
    reasons: tuple[str, ...] = field(default_factory=tuple)


class AttemptKilled(Exception):
    """Raised by a runner when its agent was terminated before finishing (e.g. SIGTERM)."""


@runtime_checkable
class GoalStore(Protocol):
    def seed(self, session_id: str, sections: Sequence[str], target_val_pass: float) -> None: ...

    def next_open(self, session_id: str) -> Goal | None:
        """The open goal to work on next (highest priority), or None when all are complete."""
        ...

    def replan(self, goal_id: str, note: str) -> None:
        """Set the goal's current strategy; next_open returns it as strategy_notes."""
        ...

    def complete(self, goal_id: str) -> None: ...


@runtime_checkable
class BriefBuilder(Protocol):
    def build(self, session_id: str, goal: Goal, use_memory: bool) -> str:
        """Bounded context for one attempt. use_memory=False is the H-mem ablation."""
        ...


@runtime_checkable
class Planner(Protocol):
    def plan(self, goal: Goal, brief: str) -> str:
        """The attempt's declared intent (also used by the observer's intent check)."""
        ...

    def replan(self, goal: Goal, brief: str) -> str:
        """A new strategy for a goal that stopped improving; becomes goal.strategy_notes."""
        ...


@runtime_checkable
class AttemptRunner(Protocol):
    agent: str

    def run(self, *, session_id: str, attempt_id: str, brief: str, intent: str) -> AttemptResult:
        """Run one headless attempt to completion. Raises AttemptKilled if terminated."""
        ...

    def reset_workspace(self, commit: str | None) -> None:
        """Discard uncommitted work and check out commit (None = the task's initial state)."""
        ...


@runtime_checkable
class Scorer(Protocol):
    def score(self) -> Score:
        """Visible + validation pass rates for the task workspace. Never the held-out split."""
        ...


@runtime_checkable
class Reviewer(Protocol):
    def review(self, result: AttemptResult, score: Score, previous: Score | None) -> Review:
        """End-of-attempt gaming review. previous is the goal's last clean score, if any."""
        ...


@runtime_checkable
class Ledger(Protocol):
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
        """Record the attempt as running before the agent starts (events reference its id)."""
        ...

    def close_attempt(
        self,
        *,
        attempt_id: str,
        result: AttemptResult,
        score: Score,
        outcome: str,
        observer_flags: Sequence[str],
    ) -> None:
        """Finish the attempt. outcome "flagged" keeps it out of memory and metrics (I8)."""
        ...

    def mark_running_as_killed(self, session_id: str) -> list[str]:
        """Mark every still-running attempt of the session as killed; return their ids."""
        ...

    def last_clean_commit(self, session_id: str) -> str | None:
        """Commit of the session's most recent closed, non-flagged attempt."""
        ...


@runtime_checkable
class Compactor(Protocol):
    def compact(self, session_id: str, goal_id: str) -> None: ...


@dataclass
class Ports:
    goals: GoalStore
    brief: BriefBuilder
    planner: Planner
    runner: AttemptRunner
    scorer: Scorer
    reviewer: Reviewer
    ledger: Ledger
    compactor: Compactor
