"""Session documents, against a throwaway Atlas database (skipped without MONGODB_URI)."""

import os
import uuid

import pytest

from tokeneyezed.data import sessions

pytestmark = pytest.mark.skipif(not os.environ.get("MONGODB_URI"), reason="MONGODB_URI not set")


@pytest.fixture
def db():
    from pymongo import MongoClient

    database = MongoClient(os.environ["MONGODB_URI"])[
        f"tokeneyezed_contract_{uuid.uuid4().hex[:10]}"
    ]
    yield database
    assert database.name.startswith("tokeneyezed_contract_")  # never drop anything else
    for name in database.list_collection_names():
        database.drop_collection(name)


def test_session_lifecycle_across_an_agent_handoff(db):
    sessions.start_session("H-1", config={"name": "H", "memory": True}, agent="claude", db=db)
    sessions.record_progress("H-1", attempt_count=2, db=db)
    sessions.end_session(
        "H-1", status=sessions.KILLED, reason="interrupted", attempt_count=2, db=db
    )
    doc = db.sessions.find_one({"session_id": "H-1"})
    assert (doc["status"], doc["agent"], doc["attempt_count"]) == ("killed", "claude", 2)
    assert doc["config"] == {"name": "H", "memory": True}

    sessions.resume_session("H-1", agent="codex", db=db)
    sessions.end_session(
        "H-1", status=sessions.FINISHED, reason="budget spent", attempt_count=5, db=db
    )
    doc = db.sessions.find_one({"session_id": "H-1"})
    assert (doc["status"], doc["reason"], doc["agent"]) == ("finished", "budget spent", "codex")
    assert [a["agent"] for a in doc["agents"]] == ["claude", "codex"]  # the handoff is recorded
    assert doc["created_at"] <= doc["last_checkpoint_at"]
