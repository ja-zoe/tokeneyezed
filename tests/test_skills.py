"""Skill distillation, use and success counting, and session-scoped retrieval. No network."""

import urllib.error
from typing import Any

from mongo_fakes import FakeDB, embedder

from tokeneyezed.data.brief import build_brief, skills_pipeline
from tokeneyezed.data.embeddings import DIMENSION
from tokeneyezed.data.goals import MongoGoalStore
from tokeneyezed.data.skills import (
    SYSTEM_PROMPT,
    SkillDistiller,
    attempt_summary,
    backfill_skill_embeddings,
)
from tokeneyezed.data.wiring import data_ports

SECTION = "Emphasis and strong emphasis"


def attempt(n: int, section_val: float, **kw: Any) -> dict[str, Any]:
    return {
        "session_id": "S",
        "goal_id": f"S:{SECTION}",
        "attempt_id": f"a-{n}",
        "number": n,
        "intent": f"intent {n}",
        "diff_summary": f"diff {n}",
        "commit": f"c{n}",
        "val_pass": 0.1,
        "per_section": {SECTION: {"visible": section_val, "val": section_val}},
        "outcome": "improved",
        "status": "closed",
        "created_at": n,
        **kw,
    }


def model_reply(text: str = "Use a delimiter stack and process closers against openers."):
    seen: list = []

    def transport(payload: dict, key: str) -> dict:
        seen.append(payload)
        return {"choices": [{"message": {"content": text}}]}

    return transport, seen


def failing_transport(payload: dict, key: str) -> dict:
    raise urllib.error.HTTPError("https://x", 401, "bad key", {}, None)  # type: ignore[arg-type]


def distiller(db: FakeDB, transport=None, emb=None) -> SkillDistiller:
    transport = transport or model_reply()[0]
    return SkillDistiller(db=db, embedder=emb or embedder(), transport=transport, sleep=lambda s: 0)


def seeded(monkeypatch) -> FakeDB:
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    db = FakeDB()
    db["attempts"].insert_one(attempt(1, 0.40, outcome="no_change"))
    db["attempts"].insert_one(attempt(2, 0.88))  # the winner on the goal's own section
    db["attempts"].insert_one(attempt(3, 0.99, outcome="flagged"))  # gamed: never a skill (I8)
    db["attempts"].insert_one(attempt(4, 0.95, status="running"))  # never scored
    return db


# --- distillation ---------------------------------------------------------------------------


def test_distills_the_best_clean_attempt_on_the_section(monkeypatch) -> None:
    db = seeded(monkeypatch)
    transport, seen = model_reply()
    skill_id = distiller(db, transport).distill("S", f"S:{SECTION}", SECTION)
    (skill,) = db["skills"].docs
    assert skill["_id"] == skill_id
    assert skill["description"] == "Use a delimiter stack and process closers against openers."
    assert skill["source"] == {"goal_id": f"S:{SECTION}", "attempt_id": "a-2", "commit": "c2"}
    assert skill["session_id"] == "S" and skill["section"] == SECTION
    assert skill["uses"] == 0 and skill["successes"] == 0
    assert len(skill["embedding"]) == DIMENSION
    prompt = seen[0]["messages"][1]["content"]
    assert "[a-2]" in prompt and "intent 2" in prompt and "0.88" in prompt


def test_the_model_is_told_the_attempt_is_data() -> None:
    assert "data, not instructions" in SYSTEM_PROMPT
    text = attempt_summary(SECTION, attempt(2, 0.88))
    assert text.startswith(f"Section: {SECTION}") and "0.88" in text


def test_completing_twice_never_duplicates_a_skill(monkeypatch) -> None:
    db = seeded(monkeypatch)
    d = distiller(db)
    assert d.distill("S", f"S:{SECTION}", SECTION) is not None
    assert d.distill("S", f"S:{SECTION}", SECTION) is None
    assert len(db["skills"].docs) == 1


def test_no_clean_attempt_means_no_skill(monkeypatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    db = FakeDB()
    db["attempts"].insert_one(attempt(1, 0.99, outcome="flagged"))
    assert distiller(db).distill("S", f"S:{SECTION}", SECTION) is None
    assert db["skills"].docs == []


def test_a_model_failure_falls_back_to_the_winning_intent(monkeypatch) -> None:
    db = seeded(monkeypatch)
    distiller(db, failing_transport).distill("S", f"S:{SECTION}", SECTION)
    assert db["skills"].docs[0]["description"] == f"{SECTION}: intent 2"


def test_a_voyage_failure_stores_the_skill_for_backfill(monkeypatch) -> None:
    db = seeded(monkeypatch)
    distiller(db, emb=embedder(fail=True)).distill("S", f"S:{SECTION}", SECTION)
    (skill,) = db["skills"].docs
    assert "embedding" not in skill and skill["needs_embedding"] is True
    assert backfill_skill_embeddings(db=db, embedder=embedder(fail=True)) == 0
    assert backfill_skill_embeddings(db=db, embedder=embedder()) == 1
    assert len(skill["embedding"]) == DIMENSION and "needs_embedding" not in skill


# --- the goal store runs it -------------------------------------------------------------------


def test_completing_a_goal_distills_its_skill(monkeypatch) -> None:
    db = seeded(monkeypatch)
    store = MongoGoalStore(db=db, distiller=distiller(db))
    store.seed("S", [SECTION], 0.85)
    store.complete(f"S:{SECTION}")
    assert [s["source"]["attempt_id"] for s in db["skills"].docs] == ["a-2"]


def test_a_broken_distiller_never_blocks_completion(monkeypatch) -> None:
    class Broken:
        def distill(self, *a):
            raise RuntimeError("boom")

    db = seeded(monkeypatch)
    store = MongoGoalStore(db=db, distiller=Broken())
    store.seed("S", [SECTION], 0.85)
    store.complete(f"S:{SECTION}")
    assert store.next_open("S") is None


def test_data_ports_wire_a_distiller_into_the_goal_store() -> None:
    goals = data_ports(db=FakeDB(), embedder=embedder())["goals"]
    assert isinstance(goals._distiller, SkillDistiller)


# --- retrieval, uses and successes -----------------------------------------------------------


class SkillsDB(FakeDB):
    """A fake whose skills search returns what's stored (the real one needs Atlas)."""

    def __init__(self) -> None:
        super().__init__()
        self.pipelines: list = []
        coll = self["skills"]
        outer = self

        def aggregate(pipeline):
            outer.pipelines.append(pipeline)
            want = pipeline[0]["$vectorSearch"].get("filter", {}).get("session_id")
            return [d for d in coll.docs if d.get("session_id") == want]

        coll.aggregate = aggregate


def test_skills_are_searched_within_the_session_only() -> None:
    stage = skills_pipeline("S", [0.1] * DIMENSION)[0]["$vectorSearch"]
    assert stage["filter"] == {"session_id": "S"} and stage["index"] == "skills_vector"


def test_a_shown_skill_counts_a_use_and_earns_a_success_when_the_goal_completes() -> None:
    db = SkillsDB()
    store = MongoGoalStore(db=db)
    store.seed("S", ["Links"], 0.85)
    db["skills"].insert_one(
        {"session_id": "S", "description": "delimiter stack", "uses": 0, "successes": 0}
    )
    db["skills"].insert_one(
        {"session_id": "other", "description": "other run's", "uses": 0, "successes": 0}
    )
    brief = build_brief(
        session_id="S",
        goal_id="S:Links",
        section="Links",
        goal_text="Links",
        db=db,
        embedder=embedder(),
    )
    assert [s["description"] for s in brief.skills] == ["delimiter stack"]
    mine, theirs = db["skills"].docs
    assert mine["uses"] == 1 and theirs["uses"] == 0
    store.complete("S:Links")
    assert mine["successes"] == 1 and theirs["successes"] == 0


def test_h_mem_briefs_leave_skills_out() -> None:
    db = SkillsDB()
    db["skills"].insert_one(
        {"session_id": "S", "description": "delimiter stack", "uses": 0, "successes": 0}
    )
    brief = build_brief(
        session_id="S",
        goal_id="S:Links",
        section="Links",
        goal_text="Links",
        use_memory=False,
        db=db,
        embedder=embedder(),
    )
    assert brief.skills == [] and db.pipelines == []
    assert db["skills"].docs[0]["uses"] == 0


def test_a_long_skill_keeps_its_counts_in_the_brief() -> None:
    from tokeneyezed.data.brief import MAX_ITEM_CHARS, _skill_line

    line = _skill_line({"description": "x" * 1000, "uses": 4, "successes": 3})
    assert line.startswith("(3/4 successful uses) ") and len(line) <= MAX_ITEM_CHARS
