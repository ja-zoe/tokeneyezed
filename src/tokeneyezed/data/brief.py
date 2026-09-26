"""Brief builder: a bounded context for the planner, assembled from Atlas (master plan, step 5).

For one goal it pulls the best attempt so far, the nearest failed attempts on this goal, relevant
memory summaries, relevant skills, and active rules. Nearest failed attempts use hybrid search
($rankFusion over the attempts vector index and the Atlas Search index), always filtered to this
session_id and goal_id.

Bounded: every section has a fixed item count and every item a fixed character cap, so the brief
stays roughly the same size however long the ledger grows. Flagged attempts never appear
(invariant I8). With use_memory=False (the H-mem run) the brief skips ledger retrieval and memory
summaries.

Nothing here blocks the loop: if the query embedding fails, the brief falls back to keyword search
for failed attempts and leaves out memory and skills.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pymongo.database import Database

from tokeneyezed.data.db import get_db
from tokeneyezed.data.embeddings import Embedder, EmbeddingError, get_embedder
from tokeneyezed.data.writes import CLOSED, FLAGGED_OUTCOME

SUCCESS_OUTCOME = "improved"  # any other outcome except flagged counts as a failed attempt

K_FAILED = 3
K_MEMORY = 3
K_SKILLS = 2
K_RULES = 5
NUM_CANDIDATES = 100
MAX_ITEM_CHARS = 300
MAX_GOAL_CHARS = 600
CHARS_PER_TOKEN = 4  # rough, for the context-size metric only

ATTEMPT_FIELDS = {
    "_id": 0,
    "attempt_id": 1,
    "intent": 1,
    "diff_summary": 1,
    "commit": 1,
    "visible_pass": 1,
    "val_pass": 1,
    "outcome": 1,
}


@dataclass
class Brief:
    goal_text: str
    strategy_notes: str = ""  # the goal's current strategy; shown, never used as a search query
    best_attempt: dict[str, Any] | None = None
    failed_attempts: list[dict[str, Any]] = field(default_factory=list)
    memories: list[dict[str, Any]] = field(default_factory=list)
    skills: list[dict[str, Any]] = field(default_factory=list)
    rules: list[dict[str, Any]] = field(default_factory=list)

    def render(self) -> str:
        sections = [
            (
                "Goal",
                [_clip(self.goal_text, MAX_GOAL_CHARS)]
                + (
                    [
                        _clip(
                            f"Current strategy (latest replan): {self.strategy_notes}",
                            MAX_GOAL_CHARS,
                        )
                    ]
                    if self.strategy_notes
                    else []
                ),
            ),
            (
                "Best attempt so far",
                [_attempt_line(self.best_attempt)] if self.best_attempt else [],
            ),
            (
                "Nearest failed attempts on this goal (don't repeat these)",
                [_attempt_line(a) for a in self.failed_attempts[:K_FAILED]],
            ),
            (
                "Memory",
                [_clip(m.get("summary", ""), MAX_ITEM_CHARS) for m in self.memories[:K_MEMORY]],
            ),
            ("Skills", [_skill_line(s) for s in self.skills[:K_SKILLS]]),
            (
                "Active rules (the observer blocks these)",
                [_rule_line(r) for r in self.rules[:K_RULES]],
            ),
        ]
        parts = []
        for title, lines in sections:
            body = "\n".join(f"- {line}" for line in lines) if lines else "- none"
            parts.append(f"## {title}\n{body}")
        return "\n\n".join(parts) + "\n"

    def approx_tokens(self) -> int:
        """Rough context size, for the flat-vs-growing context metric."""
        return len(self.render()) // CHARS_PER_TOKEN + 1


def build_brief(
    *,
    session_id: str,
    goal_id: str,
    section: str,
    goal_text: str,
    strategy_notes: str = "",
    use_memory: bool = True,
    db: Database | None = None,
    embedder: Embedder | None = None,
) -> Brief:
    """Assemble the brief for one goal.

    `goal_text` is what every search (vector, keyword, memory, skills) queries with: normally the
    spec section. `strategy_notes` is the goal's current strategy from the latest replan: shown to
    the planner under the goal, but kept out of the queries, so retrieval keeps matching the
    section rather than the wording of a replan note.

    `section` is the goal's spec section: "best" means the highest validation pass rate on that
    section (the same score the controller uses to decide an attempt improved), not overall.
    """
    for name, value in (
        ("session_id", session_id),
        ("goal_id", goal_id),
        ("section", section),
        ("goal_text", goal_text),
    ):
        if not isinstance(value, str) or not value:
            raise ValueError(f"{name} must be a non-empty string, got {value!r}")
    db = db if db is not None else get_db()
    emb = embedder or get_embedder()

    try:
        query_vector: list[float] | None = emb.embed_query_cached(goal_text)
    except (EmbeddingError, ValueError, OSError):
        query_vector = None

    brief = Brief(goal_text=goal_text, strategy_notes=strategy_notes or "")
    brief.best_attempt = db["attempts"].find_one(
        best_filter(session_id, goal_id),
        ATTEMPT_FIELDS,
        sort=[(f"per_section.{section}.val", -1), ("created_at", -1)],
    )
    if use_memory:
        brief.failed_attempts = list(
            db["attempts"].aggregate(
                failed_attempts_pipeline(session_id, goal_id, goal_text, query_vector)
            )
        )
        if query_vector is not None:
            brief.memories = list(db["memory"].aggregate(memory_pipeline(session_id, query_vector)))
    if query_vector is not None:
        brief.skills = list(db["skills"].aggregate(skills_pipeline(query_vector)))
    brief.rules = list(
        db["rules"]
        .find({"status": "active"}, {"_id": 0, "pattern": 1, "check_type": 1, "version": 1})
        .sort("version", -1)
        .limit(K_RULES)
    )
    return brief


def best_filter(session_id: str, goal_id: str) -> dict[str, Any]:
    """Scored, unflagged attempts only: never a running attempt or one killed by a resume."""
    return {
        "session_id": session_id,
        "goal_id": goal_id,
        "status": CLOSED,
        "outcome": {"$ne": FLAGGED_OUTCOME},
    }


def failed_filter(session_id: str, goal_id: str) -> dict[str, Any]:
    return {
        "session_id": session_id,
        "goal_id": goal_id,
        "status": CLOSED,  # running attempts have no outcome and killed ones were never scored
        "outcome": {"$nin": [SUCCESS_OUTCOME, FLAGGED_OUTCOME]},
    }


def failed_attempts_pipeline(
    session_id: str, goal_id: str, text: str, query_vector: list[float] | None
) -> list[dict[str, Any]]:
    """Hybrid search for failed attempts on this goal, or keyword-only without a query vector."""
    scope = failed_filter(session_id, goal_id)
    # $search can't pre-filter on fields the text index doesn't map, so it filters after matching.
    keyword = [
        {
            "$search": {
                "index": "attempts_text",
                "text": {"query": text, "path": ["intent", "diff_summary"]},
            }
        },
        {"$match": scope},
        {"$limit": NUM_CANDIDATES},
    ]
    project = {"$project": ATTEMPT_FIELDS}
    if query_vector is None:
        return [*keyword[:2], {"$limit": K_FAILED}, project]
    vector = [
        {
            "$vectorSearch": {
                "index": "attempts_vector",
                "path": "embedding",
                "queryVector": query_vector,
                "numCandidates": NUM_CANDIDATES,
                "limit": NUM_CANDIDATES,
                "filter": scope,
            }
        }
    ]
    return [
        {"$rankFusion": {"input": {"pipelines": {"vector": vector, "keyword": keyword}}}},
        {"$limit": K_FAILED},
        project,
    ]


def memory_pipeline(session_id: str, query_vector: list[float]) -> list[dict[str, Any]]:
    return [
        {
            "$vectorSearch": {
                "index": "memory_vector",
                "path": "embedding",
                "queryVector": query_vector,
                "numCandidates": NUM_CANDIDATES,
                "limit": K_MEMORY,
                "filter": {"session_id": session_id},
            }
        },
        {"$project": {"_id": 0, "summary": 1}},
    ]


def skills_pipeline(query_vector: list[float]) -> list[dict[str, Any]]:
    """Skills are reusable across sessions, so this search is not scoped."""
    return [
        {
            "$vectorSearch": {
                "index": "skills_vector",
                "path": "embedding",
                "queryVector": query_vector,
                "numCandidates": NUM_CANDIDATES,
                "limit": K_SKILLS,
            }
        },
        {"$project": {"_id": 0, "description": 1, "uses": 1, "successes": 1}},
    ]


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _rate(value: Any) -> str:
    return f"{value:.2f}" if isinstance(value, int | float) else "?"


def _attempt_line(a: dict[str, Any]) -> str:
    head = (
        f"[{a.get('attempt_id', '?')}] {a.get('outcome', '?')}, val {_rate(a.get('val_pass'))}, "
        f"visible {_rate(a.get('visible_pass'))}, commit {a.get('commit', '?')}: "
    )
    detail = a.get("intent", "")
    if a.get("diff_summary"):
        detail += f" / changed: {a['diff_summary']}"
    return _clip(head + detail, MAX_ITEM_CHARS)


def _skill_line(s: dict[str, Any]) -> str:
    uses, successes = s.get("uses") or 0, s.get("successes") or 0
    return _clip(f"{s.get('description', '')} ({successes}/{uses} successful uses)", MAX_ITEM_CHARS)


def _rule_line(r: dict[str, Any]) -> str:
    return _clip(f"{r.get('check_type', '?')}: {r.get('pattern', '')}", MAX_ITEM_CHARS)


def _goal_text(goal: Any) -> str:
    """The spec section plus the goal's current strategy, if a replan has set one."""
    notes = getattr(goal, "strategy_notes", "")
    return f"{goal.section}\nCurrent strategy: {notes}" if notes else goal.section


class MongoBriefBuilder:
    """The controller's BriefBuilder port (tokeneyezed/ports.py), backed by Atlas."""

    def __init__(self, db: Database | None = None, embedder: Embedder | None = None) -> None:
        self._db = db
        self._embedder = embedder

    def build(self, session_id: str, goal: Any, use_memory: bool) -> str:
        brief = build_brief(
            session_id=session_id,
            goal_id=goal.goal_id,
            section=goal.section,
            goal_text=goal.section,
            strategy_notes=self._strategy_notes(goal),
            use_memory=use_memory,
            db=self._db,
            embedder=self._embedder,
        )
        return brief.render()

    def _strategy_notes(self, goal: Any) -> str:
        """The goal's current strategy: from the Goal once it carries one (PR #14), else `goals`."""
        if hasattr(goal, "strategy_notes"):
            return goal.strategy_notes or ""
        db = self._db if self._db is not None else get_db()
        doc = db["goals"].find_one({"goal_id": goal.goal_id}, {"strategy_notes": 1})
        return (doc.get("strategy_notes") or "") if doc else ""
