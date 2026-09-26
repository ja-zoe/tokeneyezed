"""Write helpers for `sessions`: one document per run (master plan, "MongoDB data model").

The controller's CLI calls these; they record which run a session is (B, H, H-mem), which agent is
driving it (it changes on an agent handoff), its status, and when it last made progress. The report
uses `config.name` to label sessions; `last_checkpoint_at` is what a heartbeat watchdog would read.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pymongo.database import Database

from tokeneyezed.data.db import get_db

RUNNING, FINISHED, KILLED = "running", "finished", "killed"


def _sessions(db: Database | None):
    return (db if db is not None else get_db())["sessions"]


def _now() -> datetime:
    return datetime.now(UTC)


def start_session(
    session_id: str, *, config: dict[str, Any], agent: str, db: Database | None = None
) -> None:
    """Record a new session as running. `config` is the run config (name, memory, models, ...)."""
    now = _now()
    _sessions(db).update_one(
        {"session_id": session_id},
        {
            "$set": {
                "status": RUNNING,
                "agent": agent,
                "config": config,
                "last_checkpoint_at": now,
            },
            "$setOnInsert": {"session_id": session_id, "created_at": now},
            "$push": {"agents": {"agent": agent, "at": now}},
        },
        upsert=True,
    )


def resume_session(session_id: str, *, agent: str, db: Database | None = None) -> None:
    """Record a resume, possibly on a different agent (the agent handoff)."""
    now = _now()
    _sessions(db).update_one(
        {"session_id": session_id},
        {
            "$set": {"status": RUNNING, "agent": agent, "last_checkpoint_at": now},
            "$push": {"agents": {"agent": agent, "at": now}},
        },
        upsert=True,
    )


def record_progress(session_id: str, *, attempt_count: int, db: Database | None = None) -> None:
    _sessions(db).update_one(
        {"session_id": session_id},
        {"$set": {"attempt_count": attempt_count, "last_checkpoint_at": _now()}},
    )


def end_session(
    session_id: str, *, status: str, reason: str, attempt_count: int, db: Database | None = None
) -> None:
    """Record how the session ended: FINISHED (with the reason) or KILLED (resumable)."""
    _sessions(db).update_one(
        {"session_id": session_id},
        {
            "$set": {
                "status": status,
                "reason": reason,
                "attempt_count": attempt_count,
                "last_checkpoint_at": _now(),
            }
        },
    )
