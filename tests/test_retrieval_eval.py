"""Retrieval eval: ground truth, recall math, query scoping, auto-arm guards. No network."""

from typing import Any

import pytest
from pymongo.errors import OperationFailure

from tokeneyezed.data import retrieval_eval as re_
from tokeneyezed.data.embeddings import DIMENSION, MODEL, Embedder, RateLimiter, TokenBudget
from tokeneyezed.data.retrieval_eval import (
    AUTO_TEXT_FIELD,
    K,
    auto_index_definition,
    auto_pipeline,
    hybrid_pipeline,
    keyword_pipeline,
    recall_at_k,
    relevant_ids,
    run_eval,
    section_resolver,
    vector_pipeline,
)
from tokeneyezed.data.writes import attempt_embedding_text


def attempt(n: int, section: str, **kw: Any) -> dict[str, Any]:
    return {
        "_id": n,
        "session_id": "S",
        "attempt_id": f"a-{n}",
        "number": n,
        "goal_id": f"S:{section}",
        "intent": f"intent {n}",
        "diff_summary": f"diff {n}",
        "outcome": "no_change",
        "status": "closed",
        **kw,
    }


def by_suffix(goal_id: str) -> str:
    return goal_id.split(":", 1)[1]


# --- ground truth (validity check 1: hand-picked attempts on a shared section) -------------


def test_hand_picked_attempts_on_the_same_section_are_relevant() -> None:
    a1, a2, a3 = attempt(1, "Emphasis"), attempt(2, "Links"), attempt(3, "Emphasis")
    a4 = attempt(4, "Emphasis")
    assert relevant_ids(a4, [a1, a2, a3, a4], by_suffix) == {"a-1", "a-3"}


def test_only_earlier_attempts_count_and_never_the_query_itself() -> None:
    a1, a2, a3 = attempt(1, "Emphasis"), attempt(2, "Emphasis"), attempt(3, "Emphasis")
    assert relevant_ids(a2, [a1, a2, a3], by_suffix) == {"a-1"}
    assert relevant_ids(a1, [a1, a2, a3], by_suffix) == set()


def test_unknown_section_has_no_relevant_attempts() -> None:
    q = attempt(2, "Emphasis", goal_id="no-colon")
    assert relevant_ids(q, [attempt(1, "Emphasis"), q], lambda g: None) == set()


class Goals:
    def __init__(self, docs: dict[str, str]) -> None:
        self.docs, self.calls = docs, 0

    def find_one(self, query: dict, projection: dict) -> dict | None:
        self.calls += 1
        section = self.docs.get(query["goal_id"])
        return {"section": section} if section else None


def test_section_comes_from_goals_then_the_goal_id_and_is_cached() -> None:
    goals = Goals({"g-7": "Tabs"})
    resolve = section_resolver({"goals": goals})
    assert resolve("g-7") == "Tabs"
    assert resolve("H-1:Emphasis and strong emphasis") == "Emphasis and strong emphasis"
    assert resolve("weird") is None
    resolve("g-7")
    assert goals.calls == 3


# --- recall@5 (validity check 2: always between 0 and 1) ----------------------------------


@pytest.mark.parametrize(
    ("ranked", "relevant", "expected"),
    [
        (["a", "b", "c", "d", "e"], {"a"}, 1.0),
        (["x", "y", "z", "w", "v"], {"a"}, 0.0),
        (["a", "x", "b", "y", "z"], {"a", "b", "c", "d"}, 0.5),
        (["a", "b", "c", "d", "e", "f"], set("abcdefgh"), 1.0),  # capped at K relevant
        (["x", "a"], {"a", "b"}, 0.5),
        ([], {"a"}, 0.0),
    ],
)
def test_recall_at_5(ranked: list[str], relevant: set[str], expected: float) -> None:
    value = recall_at_k(ranked, relevant)
    assert value == pytest.approx(expected) and 0 <= value <= 1


def test_recall_ignores_hits_beyond_rank_5() -> None:
    assert recall_at_k(["x"] * K + ["a"], {"a"}) == 0.0


def test_recall_needs_relevant_items() -> None:
    with pytest.raises(ValueError):
        recall_at_k(["a"], set())


# --- query construction -------------------------------------------------------------------


def test_every_arm_is_scoped_to_the_session() -> None:
    vec = vector_pipeline("S", [0.1] * DIMENSION)
    assert vec[0]["$vectorSearch"]["filter"] == {"session_id": "S"}
    assert vec[0]["$vectorSearch"]["index"] == "attempts_vector"
    kw = keyword_pipeline("S", "emphasis")
    assert kw[0]["$search"]["index"] == "attempts_text" and kw[1] == {"$match": {"session_id": "S"}}
    fusion = hybrid_pipeline("S", "emphasis", [0.1] * DIMENSION)[0]["$rankFusion"]["input"]
    assert set(fusion["pipelines"]) == {"vector", "keyword"}
    assert fusion["pipelines"]["vector"][0]["$vectorSearch"]["filter"] == {"session_id": "S"}
    auto = auto_pipeline("S", "emphasis")[0]["$vectorSearch"]
    assert auto["filter"] == {"session_id": "S"} and auto["query"] == "emphasis"
    assert "queryVector" not in auto  # Automated Embedding embeds the text itself


def test_auto_index_uses_embed_models_model_on_the_same_text() -> None:
    (embed_field, filter_field) = auto_index_definition()["fields"]
    assert embed_field == {
        "type": "autoEmbed",
        "modality": "text",
        "path": AUTO_TEXT_FIELD,
        "model": MODEL,
    }
    assert filter_field == {"type": "filter", "path": "session_id"}


def test_auto_copy_holds_the_exact_text_embed_embeds() -> None:
    inserted: list = []

    class Copy:
        def delete_many(self, q: dict) -> None:
            pass

        def insert_many(self, docs: list) -> None:
            inserted.extend(docs)

    a = attempt(1, "Emphasis", intent="Use a delimiter stack", diff_summary="inline.py")
    assert re_.sync_auto_copy({re_.ATTEMPTS_AUTO: Copy()}, "S", [a]) == 1
    assert inserted[0][AUTO_TEXT_FIELD] == attempt_embedding_text(a["intent"], a["diff_summary"])
    assert inserted[0]["_id"] == a["_id"] and inserted[0]["session_id"] == "S"


def test_auto_arm_refuses_a_dimension_it_cannot_match(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(re_, "DIMENSION", 512)
    with pytest.raises(RuntimeError, match="different embeddings"):
        re_.prepare_auto_arm("S", db=object())


# --- run_eval against a fake database -----------------------------------------------------


class Attempts:
    def __init__(self, docs: list[dict], results: dict[str, list[str]]) -> None:
        self.docs, self.results, self.pipelines = docs, results, []

    def find(self, query: dict, projection: dict) -> list[dict]:
        return [d for d in self.docs if d["outcome"] not in ("flagged", "killed")]

    def aggregate(self, pipeline: list) -> list[dict]:
        self.pipelines.append(pipeline)
        stage = next(iter(pipeline[0]))
        arm = {"$vectorSearch": "vector", "$search": "keyword", "$rankFusion": "hybrid"}[stage]
        return [{"attempt_id": i} for i in self.results[arm]]


class Auto:
    def __init__(self, ids: list[str], fail_times: int = 0) -> None:
        self.ids, self.fail_times, self.calls = ids, fail_times, 0

    def aggregate(self, pipeline: list) -> list[dict]:
        self.calls += 1
        if self.calls <= self.fail_times:
            raise OperationFailure("Rate limit exceeded for autoEmbed queries")
        return [{"attempt_id": i} for i in self.ids]


class NoGoals:
    def find_one(self, *a: Any) -> None:
        return None


def embedder(payloads: list) -> Embedder:
    def transport(payload: dict) -> dict:
        payloads.append(payload)
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


def fake_db(auto: Auto | None = None) -> tuple[dict, Attempts]:
    docs = [
        attempt(1, "Emphasis"),
        attempt(2, "Links"),
        attempt(3, "Emphasis", outcome="flagged"),
        attempt(4, "Emphasis"),
    ]
    # later attempts (a-4) and flagged ones (a-3) come back from search but must not count
    results = {
        "vector": ["a-4", "a-3", "a-1", "a-2"],
        "keyword": ["a-2", "a-4"],
        "hybrid": ["a-1", "a-2"],
    }
    att = Attempts(docs, results)
    return {
        "attempts": att,
        "goals": NoGoals(),
        re_.ATTEMPTS_AUTO: auto or Auto(["a-2", "a-1"]),
    }, att


def test_run_eval_scores_each_arm_on_earlier_attempts_only() -> None:
    payloads: list = []
    db, att = fake_db()
    report = run_eval("S", db=db, embedder=embedder(payloads), sleep=lambda s: None)
    (q,) = report.queries  # a-1 has no earlier attempt, a-2 has none on Links, a-3 is flagged
    assert q.attempt_id == "a-4" and q.relevant == {"a-1"}
    assert q.ranked == {
        "vector": ["a-1", "a-2"],
        "keyword": ["a-2"],
        "hybrid": ["a-1", "a-2"],
        "auto": ["a-2", "a-1"],
    }
    assert q.recall == {"vector": 1.0, "keyword": 0.0, "hybrid": 1.0, "auto": 1.0}
    assert report.skipped_no_relevant == 2
    assert [p["input_type"] for p in payloads] == ["query"]  # embedded once, cached for hybrid


def test_run_eval_retries_rate_limited_auto_queries() -> None:
    sleeps: list[float] = []
    auto = Auto(["a-1"], fail_times=2)
    db, _ = fake_db(auto)
    report = run_eval("S", db=db, embedder=embedder([]), sleep=sleeps.append)
    assert report.queries[0].recall["auto"] == 1.0 and auto.calls == 3 and len(sleeps) == 2


@pytest.mark.parametrize(
    ("message", "retried"),
    [
        ("Rate limit exceeded", True),
        ("Got non OK status from response, status code: 503", True),
        ("Got non OK status from response, status code: 429", True),
        ("Got non OK status from response, status code: 400", False),
        ("Path 'x' needs to be indexed as filter", False),
    ],
)
def test_only_transient_auto_errors_are_retried(message: str, retried: bool) -> None:
    assert re_._transient(OperationFailure(message)) is retried


def test_run_eval_rejects_unknown_arms() -> None:
    with pytest.raises(ValueError):
        run_eval("S", db={}, arms=["vector", "bm25"])


def test_report_shows_all_arms_side_by_side_and_marks_relevant() -> None:
    db, _ = fake_db()
    text = run_eval("S", db=db, embedder=embedder([]), sleep=lambda s: None).render()
    assert "Top 5 for query a-4" in text
    for arm in ("vector", "keyword", "hybrid", "auto"):
        assert f"  {arm}" in text
    assert "'a-1*'" in text and "'a-2'" in text


def test_wait_until_searchable_polls_until_complete() -> None:
    counts = iter([0, 3, 5])
    sleeps: list[float] = []
    assert re_.wait_until_searchable(lambda: next(counts), 5, sleep=sleeps.append)
    assert len(sleeps) == 2
