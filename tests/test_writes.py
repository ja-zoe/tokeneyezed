"""events/attempts write helpers. No network: fake collections and a fake Voyage transport."""

import urllib.error
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from tokeneyezed.data.embeddings import DIMENSION, Embedder, RateLimiter, TokenBudget
from tokeneyezed.data.writes import (
    FLAGGED_OUTCOME,
    backfill_embeddings,
    insert_event,
    write_attempt,
)


class FakeResult:
    def __init__(self, inserted_id: int) -> None:
        self.inserted_id = inserted_id


class FakeCursor(list):
    def limit(self, n: int) -> "FakeCursor":
        return FakeCursor(self[:n])


class FakeCollection:
    def __init__(self) -> None:
        self.docs: list[dict[str, Any]] = []

    def insert_one(self, doc: dict[str, Any]) -> FakeResult:
        doc = {**doc, "_id": len(self.docs) + 1}
        self.docs.append(doc)
        return FakeResult(doc["_id"])

    def find(self, query: dict[str, Any]) -> FakeCursor:
        def matches(doc):
            for key, value in query.items():
                if isinstance(value, dict):
                    if "$ne" in value and doc.get(key) == value["$ne"]:
                        return False
                    if "$exists" in value and (key in doc) != value["$exists"]:
                        return False
                elif doc.get(key) != value:
                    return False
            return True

        return FakeCursor(d for d in self.docs if matches(d))

    def update_one(self, query: dict[str, Any], update: dict[str, Any]):
        for doc in self.find(query):
            doc.update(update.get("$set", {}))
            for key in update.get("$unset", {}):
                doc.pop(key, None)
            return SimpleNamespace(modified_count=1)
        return SimpleNamespace(modified_count=0)


class FakeDB(dict):
    def __missing__(self, name: str) -> FakeCollection:
        self[name] = FakeCollection()
        return self[name]


def embedder(fail: bool = False) -> Embedder:
    def transport(payload: dict[str, Any]) -> dict[str, Any]:
        if fail:
            raise urllib.error.HTTPError("https://x", 401, "bad key", {}, None)  # type: ignore[arg-type]
        return {
            "data": [
                {"index": i, "embedding": [0.1] * DIMENSION} for i in range(len(payload["input"]))
            ],
            "usage": {"total_tokens": 1},
        }

    return Embedder(
        transport=transport,
        limiter=RateLimiter(1000, 4_000_000, sleep=lambda s: None),
        budget=TokenBudget(1_000_000),
        sleep=lambda s: None,
    )


EVENT = dict(
    session_id="H-0926",
    attempt_id="a-017",
    agent="claude",
    phase="pre",
    tool="bash",
    input="pip install markdown-it-py",
    output_summary=None,
    verdict="block: honeypot",
)

ATTEMPT = dict(
    session_id="H-0926",
    attempt_id="a-017",
    goal_id="g-emphasis",
    agent="claude",
    intent="Replace regex emphasis with a delimiter stack",
    diff_summary="inline.py: add delimiter stack",
    commit="3f9c2e1",
    visible_pass=0.81,
    val_pass=0.58,
    per_section={"Emphasis and strong emphasis": {"visible": 0.9, "val": 0.71}},
    outcome="improved",
    observer_flags=[],
    parent_attempt="a-014",
)


def test_insert_event_writes_every_field() -> None:
    db = FakeDB()
    ts = datetime(2026, 9, 26, tzinfo=UTC)
    insert_event(**EVENT, ts=ts, db=db)
    doc = db["events"].docs[0]
    assert {k: doc[k] for k in EVENT} == EVENT
    assert doc["ts"] == ts


def test_insert_event_defaults_ts_to_now() -> None:
    db = FakeDB()
    insert_event(**EVENT, db=db)
    assert isinstance(db["events"].docs[0]["ts"], datetime)


@pytest.mark.parametrize("field", list(EVENT))
def test_insert_event_requires_every_field(field: str) -> None:
    args = {k: v for k, v in EVENT.items() if k != field}
    with pytest.raises(TypeError):
        insert_event(**args, db=FakeDB())


@pytest.mark.parametrize(
    ("field", "value"), [("phase", "during"), ("tool", "Bash"), ("verdict", "")]
)
def test_insert_event_rejects_bad_values(field: str, value: str) -> None:
    with pytest.raises(ValueError):
        insert_event(**{**EVENT, field: value}, db=FakeDB())


def test_write_attempt_writes_every_field_and_the_embedding() -> None:
    db = FakeDB()
    write_attempt(**ATTEMPT, db=db, embedder=embedder())
    doc = db["attempts"].docs[0]
    assert {k: doc[k] for k in ATTEMPT} == ATTEMPT
    assert len(doc["embedding"]) == DIMENSION
    assert "needs_embedding" not in doc
    assert isinstance(doc["created_at"], datetime)


@pytest.mark.parametrize("field", list(ATTEMPT))
def test_write_attempt_requires_every_field(field: str) -> None:
    args = {k: v for k, v in ATTEMPT.items() if k != field}
    with pytest.raises(TypeError):
        write_attempt(**args, db=FakeDB(), embedder=embedder())


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("visible_pass", 1.5),
        ("val_pass", None),
        ("val_pass", True),
        ("per_section", None),
        ("observer_flags", None),
        ("commit", ""),
        ("parent_attempt", ""),
    ],
)
def test_write_attempt_rejects_bad_values(field: str, value: Any) -> None:
    with pytest.raises(ValueError):
        write_attempt(**{**ATTEMPT, field: value}, db=FakeDB(), embedder=embedder())


def test_first_attempt_may_have_no_parent() -> None:
    db = FakeDB()
    write_attempt(**{**ATTEMPT, "parent_attempt": None}, db=db, embedder=embedder())
    assert db["attempts"].docs[0]["parent_attempt"] is None


def test_voyage_failure_still_writes_the_attempt_and_flags_it_for_backfill() -> None:
    db = FakeDB()
    write_attempt(**ATTEMPT, db=db, embedder=embedder(fail=True))
    doc = db["attempts"].docs[0]
    assert "embedding" not in doc
    assert doc["needs_embedding"] is True
    assert {k: doc[k] for k in ATTEMPT} == ATTEMPT


def test_flagged_attempt_is_written_but_never_embedded() -> None:
    # Invariant I8: flagged attempts stay out of embeddings, but the audit record exists.
    db = FakeDB()
    write_attempt(**{**ATTEMPT, "outcome": FLAGGED_OUTCOME}, db=db, embedder=embedder())
    doc = db["attempts"].docs[0]
    assert "embedding" not in doc and "needs_embedding" not in doc


def test_backfill_embeds_only_the_queue() -> None:
    db = FakeDB()
    write_attempt(**{**ATTEMPT, "attempt_id": "a-1"}, db=db, embedder=embedder(fail=True))
    write_attempt(**{**ATTEMPT, "attempt_id": "a-2"}, db=db, embedder=embedder())
    write_attempt(
        **{**ATTEMPT, "attempt_id": "a-3", "outcome": FLAGGED_OUTCOME}, db=db, embedder=embedder()
    )

    assert backfill_embeddings(db=db, embedder=embedder(fail=True)) == 0
    assert backfill_embeddings(db=db, embedder=embedder()) == 1

    by_id = {d["attempt_id"]: d for d in db["attempts"].docs}
    assert len(by_id["a-1"]["embedding"]) == DIMENSION and "needs_embedding" not in by_id["a-1"]
    assert "embedding" not in by_id["a-3"]


@pytest.mark.parametrize("limit", [0, -1, True, 1.5])
def test_backfill_rejects_unbounded_or_invalid_limit(limit) -> None:
    with pytest.raises(ValueError):
        backfill_embeddings(limit=limit, db=FakeDB(), embedder=embedder())


def test_backfill_skips_a_queued_attempt_flagged_after_write(monkeypatch) -> None:
    db = FakeDB()
    write_attempt(**ATTEMPT, db=db, embedder=embedder(fail=True))
    doc = db["attempts"].docs[0]
    doc["outcome"] = FLAGGED_OUTCOME
    emb = embedder()

    def forbidden(text):
        pytest.fail("flagged attempts must not reach the embedding API")

    monkeypatch.setattr(emb, "embed_document_or_none", forbidden)
    assert backfill_embeddings(db=db, embedder=emb) == 0
    assert "embedding" not in doc


def test_backfill_rechecks_flag_before_storing_vector(monkeypatch) -> None:
    db = FakeDB()
    write_attempt(**ATTEMPT, db=db, embedder=embedder(fail=True))
    doc = db["attempts"].docs[0]
    emb = embedder()

    def flag_during_embedding(text):
        doc["outcome"] = FLAGGED_OUTCOME
        return [0.1] * DIMENSION

    monkeypatch.setattr(emb, "embed_document_or_none", flag_during_embedding)
    assert backfill_embeddings(db=db, embedder=emb) == 0
    assert "embedding" not in doc


def test_backfill_does_not_overwrite_an_existing_embedding() -> None:
    db = FakeDB()
    write_attempt(**ATTEMPT, db=db, embedder=embedder())
    doc = db["attempts"].docs[0]
    doc["needs_embedding"] = True
    original = doc["embedding"]
    assert backfill_embeddings(db=db, embedder=embedder()) == 0
    assert doc["embedding"] is original
