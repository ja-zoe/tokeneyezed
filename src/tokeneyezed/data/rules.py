"""MongoDB helpers for reading observer events and storing learned rules."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from pymongo import DESCENDING
from pymongo.database import Database

from tokeneyezed.data.db import get_db

DEFAULT_EVENT_LIMIT = 10_000


def load_learning_events(
    *, db: Database | None = None, limit: int = DEFAULT_EVENT_LIMIT
) -> list[dict[str, Any]]:
    """Load the most recent events, oldest first, for bounded deterministic replay."""
    if type(limit) is not int or limit < 1:
        raise ValueError("limit must be a positive integer")
    database = db if db is not None else get_db()
    events = list(database["events"].find({}).sort("ts", DESCENDING).limit(limit))
    events.reverse()
    return events


def load_rules(*, db: Database | None = None) -> list[dict[str, Any]]:
    """Read rule records for replaying prior active and candidate rules."""
    database = db if db is not None else get_db()
    return list(database["rules"].find({}))


def load_active_rules(*, db: Database | None = None) -> list[dict[str, Any]]:
    """Read only rules that passed replay and are eligible for the pre-gate."""
    database = db if db is not None else get_db()
    return list(database["rules"].find({"status": "active"}))


def store_learned_rules(
    rules: Iterable[dict[str, Any]], *, db: Database | None = None
) -> int:
    """Upsert rule results by stable pattern identity; return the number processed."""
    database = db if db is not None else get_db()
    collection = database["rules"]
    now = datetime.now(UTC)
    count = 0
    for rule in rules:
        if (
            not isinstance(rule.get("pattern"), str)
            or not rule["pattern"]
            or not isinstance(rule.get("tool"), str)
            or rule.get("check_type") != "input_contains"
            or rule.get("status") not in {"candidate", "active", "retired"}
            or type(rule.get("version")) is not int
            or rule["version"] < 1
        ):
            raise ValueError(f"invalid learned rule: {rule!r}")
        identity = {
            "tool": rule["tool"],
            "check_type": rule["check_type"],
            "pattern": rule["pattern"],
            "version": rule["version"],
        }
        collection.update_one(
            identity,
            {
                "$set": {**rule, "updated_at": now},
                "$setOnInsert": {"created_at": now},
            },
            upsert=True,
        )
        count += 1
    return count
