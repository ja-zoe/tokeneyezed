"""Attempt lifecycle in Atlas: open (running) -> close, or killed on resume. No network."""

from typing import Any

import pytest
from mongo_fakes import FakeDB, embedder

from tokeneyezed.data.writes import (
    close_attempt,
    last_clean_commit,
    mark_running_as_killed,
    open_attempt,
    restore_killed_attempt,
)

OPEN = dict(
    session_id="H-0926",
    attempt_id="a-001",
    number=1,
    goal_id="g-emphasis",
    agent="claude",
    intent="Replace regex emphasis with a delimiter stack",
    parent_attempt=None,
)

CLOSE = dict(
    attempt_id="a-001",
    diff_summary="inline.py: add delimiter stack",
    commit="3f9c2e1",
    visible_pass=0.81,
    val_pass=0.58,
    per_section={"Emphasis": {"visible": 0.9, "val": 0.71}},
    outcome="improved",
    observer_flags=[],
)


def opened(db: FakeDB, **overrides: Any) -> None:
    open_attempt(db=db, **{**OPEN, **overrides})


def test_open_attempt_writes_a_running_doc_without_scores_or_embedding() -> None:
    db = FakeDB()
    opened(db)
    (doc,) = db["attempts"].docs
    assert doc["status"] == "running" and doc["number"] == 1
    assert doc["intent"] == OPEN["intent"] and doc["parent_attempt"] is None
    assert not {"outcome", "val_pass", "commit", "embedding", "needs_embedding"} & doc.keys()


@pytest.mark.parametrize("field", list(OPEN))
def test_open_attempt_requires_every_field(field: str) -> None:
    with pytest.raises(TypeError):
        open_attempt(**{k: v for k, v in OPEN.items() if k != field})


@pytest.mark.parametrize("number", [0, -1, 1.5, True, "1"])
def test_open_attempt_rejects_a_bad_number(number: Any) -> None:
    with pytest.raises(ValueError):
        open_attempt(db=FakeDB(), **{**OPEN, "number": number})


def test_close_attempt_fills_the_doc_and_embeds_it() -> None:
    db = FakeDB()
    opened(db)
    close_attempt(db=db, embedder=embedder(), **CLOSE)
    (doc,) = db["attempts"].docs
    assert doc["status"] == "closed" and doc["outcome"] == "improved"
    assert doc["commit"] == "3f9c2e1" and doc["val_pass"] == 0.58
    assert len(doc["embedding"]) > 0 and "needs_embedding" not in doc


def test_close_attempt_survives_a_voyage_failure() -> None:
    db = FakeDB()
    opened(db)
    close_attempt(db=db, embedder=embedder(fail=True), **CLOSE)
    (doc,) = db["attempts"].docs
    assert doc["status"] == "closed" and doc["needs_embedding"] is True
    assert "embedding" not in doc


def test_close_attempt_never_embeds_a_flagged_attempt() -> None:
    db = FakeDB()
    opened(db)
    close_attempt(db=db, embedder=embedder(), **{**CLOSE, "outcome": "flagged"})
    (doc,) = db["attempts"].docs
    assert doc["status"] == "closed"
    assert "embedding" not in doc and "needs_embedding" not in doc


def test_close_attempt_needs_a_running_attempt() -> None:
    db = FakeDB()
    with pytest.raises(LookupError):
        close_attempt(db=db, embedder=embedder(), **CLOSE)  # never opened
    opened(db)
    close_attempt(db=db, embedder=embedder(), **CLOSE)
    with pytest.raises(LookupError):
        close_attempt(db=db, embedder=embedder(), **CLOSE)  # already closed


def test_a_killed_attempt_cannot_be_closed_later() -> None:
    db = FakeDB()
    opened(db)
    mark_running_as_killed("H-0926", db=db)
    with pytest.raises(LookupError):
        close_attempt(db=db, embedder=embedder(), **CLOSE)
    assert db["attempts"].docs[0]["status"] == "killed"


def test_restore_killed_attempt_for_checkpoint_recovery() -> None:
    db = FakeDB()
    opened(db)
    mark_running_as_killed("H-0926", db=db)
    assert restore_killed_attempt("a-001", db=db)
    assert not restore_killed_attempt("a-001", db=db)
    doc = db["attempts"].docs[0]
    assert doc["status"] == "running" and "outcome" not in doc and "closed_at" not in doc
    close_attempt(db=db, embedder=embedder(), **CLOSE)
    assert doc["status"] == "closed"


def test_mark_running_as_killed_is_scoped_and_idempotent() -> None:
    db = FakeDB()
    opened(db, attempt_id="a-001", number=1)
    opened(db, attempt_id="a-002", number=2)
    opened(db, attempt_id="b-001", session_id="other")
    close_attempt(db=db, embedder=embedder(), **CLOSE)  # a-001 finished normally

    assert mark_running_as_killed("H-0926", db=db) == ["a-002"]
    assert mark_running_as_killed("H-0926", db=db) == []
    by_id = {d["attempt_id"]: d for d in db["attempts"].docs}
    assert by_id["a-002"]["status"] == "killed" and by_id["a-002"]["outcome"] == "killed"
    assert "val_pass" not in by_id["a-002"]  # killed attempts are never scored
    assert by_id["a-001"]["status"] == "closed" and by_id["b-001"]["status"] == "running"


def test_last_clean_commit_is_the_latest_closed_unflagged_attempt() -> None:
    db = FakeDB()
    assert last_clean_commit("H-0926", db=db) is None
    for n, (outcome, commit) in enumerate(
        [("improved", "c1"), ("no_improvement", "c2"), ("flagged", "c3")], start=1
    ):
        opened(db, attempt_id=f"a-{n}", number=n)
        close_attempt(
            db=db,
            embedder=embedder(),
            **{**CLOSE, "attempt_id": f"a-{n}", "outcome": outcome, "commit": commit},
        )
    opened(db, attempt_id="a-4", number=4)  # running: no commit yet
    opened(db, attempt_id="a-5", number=5)
    mark_running_as_killed("H-0926", db=db)  # killed: never clean

    assert last_clean_commit("H-0926", db=db) == "c2"  # flagged c3 skipped, not the latest number
    assert last_clean_commit("other", db=db) is None


def test_close_loses_the_race_against_a_resume_that_kills_the_attempt() -> None:
    db = FakeDB()
    opened(db)
    attempts = db["attempts"]
    original_find = attempts.find

    def find_then_kill(query: dict[str, Any]):
        found = original_find(query)
        for doc in attempts.docs:
            doc["status"] = "killed"  # resume lands between close's read and its write
        return found

    attempts.find = find_then_kill  # type: ignore[method-assign]
    with pytest.raises(LookupError):
        close_attempt(db=db, embedder=embedder(), **CLOSE)
    assert "commit" not in attempts.docs[0] and attempts.docs[0]["status"] == "killed"


def test_kill_does_not_report_an_attempt_that_closed_in_the_meantime() -> None:
    db = FakeDB()
    opened(db)
    attempts = db["attempts"]
    original_find = attempts.find

    def find_then_close(query: dict[str, Any]):
        found = original_find(query)
        for doc in attempts.docs:
            doc["status"] = "closed"  # the agent finished after kill's read
        return found

    attempts.find = find_then_close  # type: ignore[method-assign]
    assert mark_running_as_killed("H-0926", db=db) == []
    assert attempts.docs[0]["status"] == "closed"
