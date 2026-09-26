"""Brief builder. No network: a fake database records queries and a fake Voyage records payloads."""

import urllib.error
from typing import Any

from tokeneyezed.data.brief import (
    K_FAILED,
    K_MEMORY,
    K_RULES,
    K_SKILLS,
    MAX_GOAL_CHARS,
    MAX_ITEM_CHARS,
    Brief,
    MongoBriefBuilder,
    build_brief,
    failed_attempts_pipeline,
)
from tokeneyezed.data.embeddings import DIMENSION, Embedder, RateLimiter, TokenBudget
from tokeneyezed.data.writes import FLAGGED_OUTCOME

ATTEMPT = {
    "attempt_id": "a-3",
    "intent": "Replace regex emphasis with a delimiter stack",
    "diff_summary": "inline.py",
    "commit": "3f9c2e1",
    "visible_pass": 0.8,
    "val_pass": 0.6,
    "outcome": "no_change",
}


class FakeCursor(list):
    def sort(self, *args: Any) -> "FakeCursor":
        return self

    def limit(self, n: int) -> "FakeCursor":
        return FakeCursor(self[:n])


class FakeCollection:
    def __init__(self, name: str, log: list) -> None:
        self.name, self.log = name, log

    def find_one(self, query: dict, projection: dict, sort: list) -> dict | None:
        self.log.append((self.name, "find_one", query))
        self.log.append((self.name, "find_one_sort", sort))
        return {**ATTEMPT, "attempt_id": "a-best", "outcome": "improved"}

    def aggregate(self, pipeline: list) -> list:
        self.log.append((self.name, "aggregate", pipeline))
        if self.name == "attempts":
            return [ATTEMPT] * 50  # more than the brief may show
        if self.name == "memory":
            return [{"summary": "Regex emphasis kept failing on nested delimiters."}]
        return [{"description": "Delimiter-run algorithm", "uses": 3, "successes": 2}]

    def find(self, query: dict, projection: dict) -> FakeCursor:
        self.log.append((self.name, "find", query))
        return FakeCursor([{"check_type": "honeypot", "pattern": "pip install mistune"}] * 20)


class FakeDB(dict):
    def __init__(self) -> None:
        super().__init__()
        self.log: list = []

    def __missing__(self, name: str) -> FakeCollection:
        self[name] = FakeCollection(name, self.log)
        return self[name]

    def calls(self, collection: str, kind: str) -> list:
        return [c[2] for c in self.log if c[0] == collection and c[1] == kind]


def embedder(payloads: list | None = None, fail: bool = False) -> Embedder:
    def transport(payload: dict[str, Any]) -> dict[str, Any]:
        if payloads is not None:
            payloads.append(payload)
        if fail:
            raise urllib.error.HTTPError("https://x", 401, "bad key", {}, None)  # type: ignore[arg-type]
        return {
            "data": [{"index": 0, "embedding": [0.1] * DIMENSION}],
            "usage": {"total_tokens": 1},
        }

    return Embedder(
        transport=transport,
        limiter=RateLimiter(1000, 4_000_000, sleep=lambda s: None),
        budget=TokenBudget(1_000_000),
        sleep=lambda s: None,
    )


def build(**overrides: Any) -> tuple[Brief, FakeDB]:
    db = FakeDB()
    emb = overrides.pop("embedder", None) or embedder()
    args = dict(
        session_id="H-1",
        goal_id="g-emphasis",
        section="Emphasis and strong emphasis",
        goal_text="Emphasis and strong emphasis",
    )
    args.update(overrides)
    brief = build_brief(**args, db=db, embedder=emb)
    return brief, db


def test_query_embedding_uses_input_type_query() -> None:
    payloads: list = []
    build(embedder=embedder(payloads))
    assert [p["input_type"] for p in payloads] == ["query"]


def test_failed_attempts_are_scoped_to_session_and_goal_on_both_search_sides() -> None:
    pipeline = failed_attempts_pipeline("H-1", "g-emphasis", "emphasis", [0.1] * DIMENSION)
    fusion = pipeline[0]["$rankFusion"]["input"]["pipelines"]
    vector_filter = fusion["vector"][0]["$vectorSearch"]["filter"]
    keyword_match = fusion["keyword"][1]["$match"]
    for scope in (vector_filter, keyword_match):
        assert scope["session_id"] == "H-1" and scope["goal_id"] == "g-emphasis"
        assert FLAGGED_OUTCOME in scope["outcome"]["$nin"]
        assert "improved" in scope["outcome"]["$nin"]


def test_best_attempt_excludes_flagged_attempts() -> None:
    _, db = build()
    (query,) = db.calls("attempts", "find_one")
    assert query == {
        "session_id": "H-1",
        "goal_id": "g-emphasis",
        "status": "closed",
        "outcome": {"$ne": FLAGGED_OUTCOME},
    }


def test_best_attempt_ranks_by_the_goals_section_score_not_overall() -> None:
    _, db = build()
    (sort,) = db.calls("attempts", "find_one_sort")
    assert sort[0] == ("per_section.Emphasis and strong emphasis.val", -1)


def test_only_closed_attempts_can_be_failed_or_best() -> None:
    # A running attempt has no outcome ($nin would match it); a killed one was never scored.
    pipeline = failed_attempts_pipeline("H-1", "g", "x", [0.1] * DIMENSION)
    fusion = pipeline[0]["$rankFusion"]["input"]["pipelines"]
    assert fusion["vector"][0]["$vectorSearch"]["filter"]["status"] == "closed"
    assert fusion["keyword"][1]["$match"]["status"] == "closed"


def test_brief_builder_port_renders_the_brief_for_a_goal() -> None:
    from tokeneyezed.ports import Goal

    goal = Goal(goal_id="g-emphasis", section="Emphasis and strong emphasis", target_val_pass=0.85)
    text = MongoBriefBuilder(db=FakeDB(), embedder=embedder()).build("H-1", goal, use_memory=True)
    assert "## Goal" in text and "Emphasis and strong emphasis" in text


def test_memory_search_is_scoped_to_the_session() -> None:
    _, db = build()
    (pipeline,) = db.calls("memory", "aggregate")
    assert pipeline[0]["$vectorSearch"]["filter"] == {"session_id": "H-1"}


def test_rendered_sections_are_labeled_by_source() -> None:
    text = build()[0].render()
    for heading in (
        "## Goal",
        "## Best attempt so far",
        "## Nearest failed attempts on this goal",
        "## Memory",
        "## Skills",
        "## Active rules",
    ):
        assert heading in text
    best = text.split("## Best attempt so far")[1].split("##")[0]
    assert "[a-best]" in best


def test_item_counts_are_capped_even_if_a_query_returns_more() -> None:
    brief, _ = build()
    text = brief.render()
    assert text.count("[a-3]") == K_FAILED
    assert text.count("honeypot:") == K_RULES


def test_brief_size_stays_flat_as_the_ledger_grows() -> None:
    long = "x" * 5000
    small = Brief(goal_text="g", failed_attempts=[ATTEMPT])
    huge = Brief(
        goal_text=long,
        best_attempt={**ATTEMPT, "intent": long},
        failed_attempts=[{**ATTEMPT, "intent": long}] * 1000,
        memories=[{"summary": long}] * 1000,
        skills=[{"description": long}] * 1000,
        rules=[{"pattern": long}] * 1000,
    )
    ceiling = (
        MAX_GOAL_CHARS + (1 + K_FAILED + K_MEMORY + K_SKILLS + K_RULES) * (MAX_ITEM_CHARS + 3) + 400
    )  # headings
    assert len(small.render()) < len(huge.render()) <= ceiling


def test_h_mem_skips_ledger_retrieval_and_memory() -> None:
    brief, db = build(use_memory=False)
    assert db.calls("attempts", "aggregate") == [] and db.calls("memory", "aggregate") == []
    assert brief.failed_attempts == [] and brief.memories == []
    assert brief.best_attempt is not None and brief.rules


def test_embedding_failure_falls_back_to_keyword_search() -> None:
    brief, db = build(embedder=embedder(fail=True))
    (pipeline,) = db.calls("attempts", "aggregate")
    assert "$search" in pipeline[0] and "$rankFusion" not in pipeline[0]
    assert pipeline[1]["$match"]["goal_id"] == "g-emphasis"
    assert db.calls("memory", "aggregate") == [] and db.calls("skills", "aggregate") == []
    assert brief.failed_attempts


def test_skills_and_memory_counts() -> None:
    brief, db = build()
    assert db.calls("skills", "aggregate")[0][0]["$vectorSearch"]["limit"] == K_SKILLS
    assert db.calls("memory", "aggregate")[0][0]["$vectorSearch"]["limit"] == K_MEMORY
