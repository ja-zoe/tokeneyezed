"""The Atlas-backed GoalStore port (tokeneyezed/ports.py): one goal per spec section per session.

Document shape (master plan, "MongoDB data model"; docs/contracts.md):

    {goal_id: "<session_id>:<section>", session_id, section, status: "open" | "complete",
     priority, completion_criteria: {val_pass}, strategy_notes, replan_count,
     last_replanned_at, created_at, completed_at}

- `priority`: lower goes first; seeded in config order.
- `strategy_notes`: the goal's current strategy, set by the latest replan; "" before the first.
  next_open returns it on the Goal, and the brief shows it to the planner, so a replan actually
  changes the next attempt's plan.
- `goal_id` is unique (data/indexes.py CLASSIC_INDEXES), so concurrent seeds can't duplicate a goal.
- A replan keeps the goal open and at its priority: the demo beat is the goal document changing in
  Atlas after a failure streak and the score improving on that goal afterward.
- `seed` is idempotent: re-seeding (e.g. after a resume) never resets progress or notes.

Satisfies the GoalStore Protocol structurally; it imports only the shared `Goal` dataclass it must
return.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from pymongo.collection import Collection
from pymongo.database import Database
from pymongo.errors import DuplicateKeyError

from tokeneyezed.data.db import get_db
from tokeneyezed.ports import Goal

OPEN, COMPLETE = "open", "complete"


def goal_id_for(session_id: str, section: str) -> str:
    """The controller's goal id convention; the retrieval eval parses the section back out."""
    return f"{session_id}:{section}"


def _require_str(name: str, value: Any) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty string, got {value!r}")


class MongoGoalStore:
    def __init__(self, db: Database | None = None) -> None:
        self._db = db

    def seed(self, session_id: str, sections: Sequence[str], target_val_pass: float) -> None:
        _require_str("session_id", session_id)
        if isinstance(sections, str) or not sections:
            raise ValueError("sections must be a non-empty list of section names")
        for section in sections:
            _require_str("section", section)
        if len(set(sections)) != len(sections):
            raise ValueError("sections must be unique: one goal per spec section")
        if (
            isinstance(target_val_pass, bool)
            or not isinstance(target_val_pass, int | float)
            or not 0 < target_val_pass <= 1
        ):
            raise ValueError(f"target_val_pass must be in (0, 1], got {target_val_pass!r}")

        now = datetime.now(UTC)
        for priority, section in enumerate(sections):
            goal_id = goal_id_for(session_id, section)
            try:
                self._goals().update_one(
                    {"goal_id": goal_id},
                    {
                        "$setOnInsert": {
                            "goal_id": goal_id,
                            "session_id": session_id,
                            "section": section,
                            "status": OPEN,
                            "priority": priority,
                            "completion_criteria": {"val_pass": target_val_pass},
                            "strategy_notes": "",
                            "replan_count": 0,
                            "last_replanned_at": None,
                            "created_at": now,
                        }
                    },
                    upsert=True,
                )
            except DuplicateKeyError:
                pass  # a concurrent seed inserted this goal first; the unique index kept it single

    def next_open(self, session_id: str) -> Goal | None:
        """The open goal with the lowest priority number, or None when every goal is complete."""
        doc = self._goals().find_one(
            {"session_id": session_id, "status": OPEN},
            {"goal_id": 1, "section": 1, "completion_criteria": 1, "strategy_notes": 1},
            sort=[("priority", 1)],
        )
        if doc is None:
            return None
        return Goal(
            goal_id=doc["goal_id"],
            section=doc["section"],
            target_val_pass=doc["completion_criteria"]["val_pass"],
            strategy_notes=doc.get("strategy_notes") or "",
        )

    def replan(self, goal_id: str, note: str) -> None:
        """Record the new strategy. The goal stays open at its priority."""
        _require_str("goal_id", goal_id)
        _require_str("note", note)
        result = self._goals().update_one(
            {"goal_id": goal_id},
            {
                "$set": {"strategy_notes": note, "last_replanned_at": datetime.now(UTC)},
                "$inc": {"replan_count": 1},
            },
        )
        if result.matched_count != 1:
            raise LookupError(f"no goal {goal_id!r}")

    def complete(self, goal_id: str) -> None:
        _require_str("goal_id", goal_id)
        result = self._goals().update_one(
            {"goal_id": goal_id},
            {"$set": {"status": COMPLETE, "completed_at": datetime.now(UTC)}},
        )
        if result.matched_count != 1:
            raise LookupError(f"no goal {goal_id!r}")

    def strategy_notes(self, goal_id: str) -> str:
        """The goal's current strategy, "" before any replan (or for an unknown goal)."""
        doc = self._goals().find_one({"goal_id": goal_id}, {"strategy_notes": 1})
        return (doc.get("strategy_notes") or "") if doc else ""

    def _goals(self) -> Collection:
        return (self._db if self._db is not None else get_db())["goals"]
