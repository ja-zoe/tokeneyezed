"""Read side of the held-out scores, for the dashboard and the final report only.

I3 keeps the collection's name inside this package, so the harness can't read it by accident. The
dashboard imports these two names instead of spelling the collection itself.
"""

from __future__ import annotations

from pymongo.database import Database

COLLECTION = "test_evals"


def held_out_by_attempt(db: Database, session_id: str) -> dict[str, float]:
    """attempt_id -> held-out pass rate for every scored attempt of the session."""
    return {
        e["attempt_id"]: e["test_pass"]
        for e in db[COLLECTION].find({"session_id": session_id})
        if "attempt_id" in e and "test_pass" in e
    }
