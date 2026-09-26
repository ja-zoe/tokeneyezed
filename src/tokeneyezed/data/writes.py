"""Write helpers for `events` and `attempts`. Import these instead of writing raw Mongo calls.

Field lists follow master-plan.md ("MongoDB data model") and docs/contracts.md. Every field is a
required keyword argument, so a caller that forgets one gets a TypeError, not a silent null.

Embeddings (master plan): embed at write time, and if Voyage fails, write the attempt anyway
without `embedding`, set `needs_embedding: True`, and let backfill_embeddings() retry later.
The agent loop never blocks on embeddings.

Flagged attempts (invariant I8) are written for the audit trail but never embedded, so they
can't surface in vector search.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from pymongo.collection import Collection
from pymongo.database import Database

from tokeneyezed.data.db import get_db
from tokeneyezed.data.embeddings import Embedder, get_embedder

PHASES = ("pre", "post", "stop")
TOOLS = ("bash", "edit", "write", "read", "other")  # neutral names, docs/contracts.md
FLAGGED_OUTCOME = "flagged"  # set by the end-of-attempt gaming review


def _now() -> datetime:
    return datetime.now(UTC)


def _require_str(name: str, value: Any) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty string, got {value!r}")


def _require_rate(name: str, value: Any) -> None:
    if isinstance(value, bool) or not isinstance(value, int | float) or not 0 <= value <= 1:
        raise ValueError(f"{name} must be a pass rate between 0 and 1, got {value!r}")


def attempt_embedding_text(intent: str, diff_summary: str) -> str:
    """What gets embedded for an attempt: what it set out to do, then what it changed."""
    return f"{intent}\n\n{diff_summary}" if diff_summary else intent


def insert_event(
    *,
    session_id: str,
    attempt_id: str,
    agent: str,
    phase: str,
    tool: str,
    input: Any,
    output_summary: str | None,
    verdict: str,
    ts: datetime | None = None,
    db: Database | None = None,
) -> Any:
    """Insert one hook event. output_summary is None in the pre phase (the tool hasn't run).

    ts defaults to now (UTC). Returns the inserted _id.
    """
    for name, value in (
        ("session_id", session_id),
        ("attempt_id", attempt_id),
        ("agent", agent),
        ("verdict", verdict),
    ):
        _require_str(name, value)
    if phase not in PHASES:
        raise ValueError(f"phase must be one of {PHASES}, got {phase!r}")
    if tool not in TOOLS:
        raise ValueError(f"tool must be one of {TOOLS} (normalize it in the shim), got {tool!r}")

    doc = {
        "session_id": session_id,
        "attempt_id": attempt_id,
        "agent": agent,
        "phase": phase,
        "tool": tool,
        "input": input,
        "output_summary": output_summary,
        "verdict": verdict,
        "ts": ts or _now(),
    }
    return _events(db).insert_one(doc).inserted_id


def write_attempt(
    *,
    session_id: str,
    attempt_id: str,
    goal_id: str,
    agent: str,
    intent: str,
    diff_summary: str,
    commit: str,
    visible_pass: float,
    val_pass: float,
    per_section: Mapping[str, Any],
    outcome: str,
    observer_flags: list[Any],
    parent_attempt: str | None,
    db: Database | None = None,
    embedder: Embedder | None = None,
) -> Any:
    """Insert one attempt into the ledger, embedded at write time. Returns the inserted _id.

    parent_attempt is None only for a goal's first attempt. Never raises on an embedding failure:
    the document is written without `embedding` and with `needs_embedding: True`.
    """
    for name, value in (
        ("session_id", session_id),
        ("attempt_id", attempt_id),
        ("goal_id", goal_id),
        ("agent", agent),
        ("intent", intent),
        ("commit", commit),
        ("outcome", outcome),
    ):
        _require_str(name, value)
    if not isinstance(diff_summary, str):
        raise ValueError(f"diff_summary must be a string, got {diff_summary!r}")
    _require_rate("visible_pass", visible_pass)
    _require_rate("val_pass", val_pass)
    if not isinstance(per_section, Mapping):
        raise ValueError(f"per_section must be a mapping, got {per_section!r}")
    if not isinstance(observer_flags, list):
        raise ValueError(f"observer_flags must be a list, got {observer_flags!r}")
    if parent_attempt is not None:
        _require_str("parent_attempt", parent_attempt)

    doc: dict[str, Any] = {
        "session_id": session_id,
        "attempt_id": attempt_id,
        "goal_id": goal_id,
        "agent": agent,
        "intent": intent,
        "diff_summary": diff_summary,
        "commit": commit,
        "visible_pass": visible_pass,
        "val_pass": val_pass,
        "per_section": dict(per_section),
        "outcome": outcome,
        "observer_flags": observer_flags,
        "parent_attempt": parent_attempt,
        "created_at": _now(),
    }
    if outcome != FLAGGED_OUTCOME:
        vector = (embedder or get_embedder()).embed_document_or_none(
            attempt_embedding_text(intent, diff_summary)
        )
        if vector is None:
            doc["needs_embedding"] = True
        else:
            doc["embedding"] = vector
    return _attempts(db).insert_one(doc).inserted_id


def backfill_embeddings(
    *, limit: int = 100, db: Database | None = None, embedder: Embedder | None = None
) -> int:
    """Embed attempts written while Voyage was failing. Returns how many were fixed."""
    attempts = _attempts(db)
    emb = embedder or get_embedder()
    fixed = 0
    for doc in attempts.find({"needs_embedding": True}).limit(limit):
        vector = emb.embed_document_or_none(
            attempt_embedding_text(doc["intent"], doc["diff_summary"])
        )
        if vector is None:
            break  # Voyage is still down; try again later
        attempts.update_one(
            {"_id": doc["_id"]},
            {"$set": {"embedding": vector}, "$unset": {"needs_embedding": ""}},
        )
        fixed += 1
    return fixed


def _events(db: Database | None) -> Collection:
    return (db if db is not None else get_db())["events"]


def _attempts(db: Database | None) -> Collection:
    return (db if db is not None else get_db())["attempts"]
