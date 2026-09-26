"""Retrieval eval: recall@5 at surfacing earlier attempts on the same spec section.

Master plan, "Evaluation plan" -> Retrieval / embeddings. Four arms answer the same queries:

- vector:  Voyage direct. The query intent goes through embed() (input_type="query") and
           $vectorSearch runs on attempts.embedding (attempts_vector).
- keyword: Atlas Search on intent + diff_summary (attempts_text).
- hybrid:  $rankFusion of the two.
- auto:    a copy of the attempts in ATTEMPTS_AUTO with an Automated Embedding index over the same
           text embed() embeds (intent + diff_summary), with the same Voyage model. It settles
           whether the embedding path (direct vs. Automated) changes accuracy.

Queries and ground truth. Each closed, unflagged attempt of a session is one query (its intent).
The candidates are the attempts that came *earlier* in the same session, excluding the query itself.
An earlier attempt is relevant when it is on the same spec section, which comes from the goal
(`goals.section`, or the controller's `<session>:<section>` goal id). Flagged attempts are left out
of queries and candidates alike (invariant I8): they're never embedded, so they could only ever
help the keyword arm. Queries with no relevant earlier attempt are skipped: recall is undefined.

recall@5 = relevant attempts in the top 5 / min(relevant attempts, 5), so a query with more than 5
relevant earlier attempts can still reach 1.0.

Cost: one Voyage query embedding per query for the vector and hybrid arms (cached), one Automated
Embedding query per query for the auto arm, and the copy's one-time initial sync.

Run it: `python -m tokeneyezed.data.retrieval_eval <session_id>` (needs MONGODB_URI and
VOYAGE_API_KEY).
"""

from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from pymongo.database import Database
from pymongo.errors import OperationFailure
from pymongo.operations import SearchIndexModel

from tokeneyezed.data.db import get_db
from tokeneyezed.data.embeddings import DIMENSION, MODEL, Embedder, get_embedder
from tokeneyezed.data.writes import FLAGGED_OUTCOME, attempt_embedding_text

K = 5
FETCH = 100  # results per arm before the earlier-than-query filter is applied
ARMS = ("vector", "keyword", "hybrid", "auto")
ATTEMPTS_AUTO = "attempts_autoembed_eval"
AUTO_INDEX = "attempts_autoembed"
AUTO_TEXT_FIELD = "embed_text"
AUTO_MODEL_DEFAULT_DIMENSION = 1024  # voyage-4's default output; autoEmbed can't set a dimension


def auto_index_definition(model: str = MODEL) -> dict[str, Any]:
    """Automated Embedding over the text embed() embeds, with the model embed() uses."""
    return {
        "fields": [
            {"type": "autoEmbed", "modality": "text", "path": AUTO_TEXT_FIELD, "model": model},
            {"type": "filter", "path": "session_id"},
        ]
    }


# --- ground truth ---------------------------------------------------------------------------


def section_resolver(db: Database) -> Callable[[str], str | None]:
    """goal_id -> spec section, from `goals`, falling back to the `<session>:<section>` id."""
    cache: dict[str, str | None] = {}

    def resolve(goal_id: str) -> str | None:
        if goal_id not in cache:
            goal = db["goals"].find_one({"goal_id": goal_id}, {"section": 1})
            if goal and goal.get("section"):
                cache[goal_id] = goal["section"]
            else:
                cache[goal_id] = goal_id.split(":", 1)[1] if ":" in goal_id else None
        return cache[goal_id]

    return resolve


def order_key(attempt: Mapping[str, Any]) -> tuple:
    """Chronological order: the ledger's attempt number, then creation time."""
    return (attempt.get("number", 0), str(attempt.get("created_at") or ""))


def relevant_ids(
    query: Mapping[str, Any],
    attempts: Sequence[Mapping[str, Any]],
    section_of: Callable[[str], str | None],
) -> set[str]:
    """Earlier attempts (not the query itself) on the query's spec section."""
    section = section_of(query["goal_id"])
    if section is None:
        return set()
    return {
        a["attempt_id"]
        for a in attempts
        if order_key(a) < order_key(query)
        and a["attempt_id"] != query["attempt_id"]
        and section_of(a["goal_id"]) == section
    }


def earlier_ids(query: Mapping[str, Any], attempts: Sequence[Mapping[str, Any]]) -> set[str]:
    return {
        a["attempt_id"]
        for a in attempts
        if order_key(a) < order_key(query) and a["attempt_id"] != query["attempt_id"]
    }


def recall_at_k(ranked: Sequence[str], relevant: set[str], k: int = K) -> float:
    if not relevant:
        raise ValueError("recall is undefined without relevant items")
    return len(set(ranked[:k]) & relevant) / min(len(relevant), k)


def load_attempts(db: Database, session_id: str) -> list[dict[str, Any]]:
    """Closed (or pre-status), unflagged attempts of the session, in chronological order."""
    docs = db["attempts"].find(
        {
            "session_id": session_id,
            "status": {"$nin": ["running", "killed"]},
            "outcome": {"$exists": True, "$nin": [FLAGGED_OUTCOME, "killed"]},
        },
        {"embedding": 0},
    )
    return sorted(docs, key=order_key)


# --- the four arms --------------------------------------------------------------------------


def vector_pipeline(session_id: str, query_vector: list[float]) -> list[dict[str, Any]]:
    return [
        {
            "$vectorSearch": {
                "index": "attempts_vector",
                "path": "embedding",
                "queryVector": query_vector,
                "numCandidates": FETCH * 4,
                "limit": FETCH,
                "filter": {"session_id": session_id},
            }
        },
        {"$project": {"_id": 0, "attempt_id": 1}},
    ]


def keyword_stages(session_id: str, text: str) -> list[dict[str, Any]]:
    # attempts_text doesn't map session_id, so the session scope is a $match after $search.
    return [
        {
            "$search": {
                "index": "attempts_text",
                "text": {"query": text, "path": ["intent", "diff_summary"]},
            }
        },
        {"$match": {"session_id": session_id}},
        {"$limit": FETCH},
    ]


def keyword_pipeline(session_id: str, text: str) -> list[dict[str, Any]]:
    return [*keyword_stages(session_id, text), {"$project": {"_id": 0, "attempt_id": 1}}]


def hybrid_pipeline(session_id: str, text: str, query_vector: list[float]) -> list[dict[str, Any]]:
    vector = vector_pipeline(session_id, query_vector)[:1]
    return [
        {
            "$rankFusion": {
                "input": {
                    "pipelines": {"vector": vector, "keyword": keyword_stages(session_id, text)}
                }
            }
        },
        {"$limit": FETCH},
        {"$project": {"_id": 0, "attempt_id": 1}},
    ]


def auto_pipeline(session_id: str, text: str) -> list[dict[str, Any]]:
    return [
        {
            "$vectorSearch": {
                "index": AUTO_INDEX,
                "path": AUTO_TEXT_FIELD,
                "query": text,
                "numCandidates": FETCH * 4,
                "limit": FETCH,
                "filter": {"session_id": session_id},
            }
        },
        {"$project": {"_id": 0, "attempt_id": 1}},
    ]


# --- the Automated Embedding copy ---------------------------------------------------------


def sync_auto_copy(db: Database, session_id: str, attempts: Sequence[Mapping[str, Any]]) -> int:
    """Copy the session's attempts (same _id, same text embed() embeds) into ATTEMPTS_AUTO.

    Idempotent: rewrites the session's copy. Returns how many documents it holds.
    """
    copy = db[ATTEMPTS_AUTO]
    copy.delete_many({"session_id": session_id})
    docs = [
        {
            "_id": a["_id"],
            "attempt_id": a["attempt_id"],
            "session_id": session_id,
            AUTO_TEXT_FIELD: attempt_embedding_text(a["intent"], a.get("diff_summary", "")),
        }
        for a in attempts
    ]
    if docs:
        copy.insert_many(docs)
    return len(docs)


def ensure_auto_index(db: Database, model: str = MODEL) -> str:
    """Create the Automated Embedding index, or fix a drifted one. Returns the action taken.

    Created after the copy is filled: the initial build isn't rate limited, later inserts are.
    """
    if ATTEMPTS_AUTO not in db.list_collection_names():
        db.create_collection(ATTEMPTS_AUTO)
    definition = auto_index_definition(model)
    live = {i["name"]: i for i in db[ATTEMPTS_AUTO].list_search_indexes()}
    if AUTO_INDEX not in live:
        db[ATTEMPTS_AUTO].create_search_index(
            SearchIndexModel(definition=definition, name=AUTO_INDEX, type="vectorSearch")
        )
        return "created"
    if not _same_auto_definition(live[AUTO_INDEX].get("latestDefinition", {}), definition):
        db[ATTEMPTS_AUTO].update_search_index(AUTO_INDEX, definition)
        return "updated"
    return "unchanged"


def _same_auto_definition(live: Mapping[str, Any], wanted: Mapping[str, Any]) -> bool:
    def norm(d: Mapping[str, Any]) -> list[tuple]:
        return sorted(tuple(sorted(f.items())) for f in d.get("fields", []))

    return norm(live) == norm(wanted)


def auto_index_model(db: Database) -> str | None:
    """The model the live Automated Embedding index actually uses, read back from Atlas."""
    for index in db[ATTEMPTS_AUTO].list_search_indexes():
        if index["name"] == AUTO_INDEX:
            for f in index.get("latestDefinition", {}).get("fields", []):
                if f.get("type") == "autoEmbed":
                    return f.get("model")
    return None


def wait_until_searchable(
    count: Callable[[], int],
    expected: int,
    *,
    ready: Callable[[], bool] = lambda: True,
    timeout: float = 900,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """Wait until the index is READY and a probe query returns every expected document."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if ready() and count() >= expected:
                return True
        except OperationFailure:
            pass  # index still building
        sleep(10)
    return False


def _index_ready(db: Database, collection: str, name: str) -> Callable[[], bool]:
    def ready() -> bool:
        for index in db[collection].list_search_indexes():
            if index["name"] == name:
                return index.get("status") == "READY" and bool(index.get("queryable"))
        return False

    return ready


# --- running it -----------------------------------------------------------------------------


@dataclass
class QueryResult:
    attempt_id: str
    section: str | None
    relevant: set[str]
    ranked: dict[str, list[str]]  # arm -> top-K attempt ids (earlier attempts only)
    recall: dict[str, float]


@dataclass
class Report:
    session_id: str
    queries: list[QueryResult] = field(default_factory=list)
    skipped_no_relevant: int = 0
    arms: tuple[str, ...] = ARMS
    notes: list[str] = field(default_factory=list)

    def mean_recall(self, arm: str) -> float | None:
        values = [q.recall[arm] for q in self.queries if arm in q.recall]
        return sum(values) / len(values) if values else None

    def render(self, example: str | None = None) -> str:
        lines = [
            f"Retrieval eval, session {self.session_id}: recall@{K} over {len(self.queries)} "
            f"queries ({self.skipped_no_relevant} skipped: no relevant earlier attempt)",
        ]
        for arm in self.arms:
            mean = self.mean_recall(arm)
            lines.append(f"  {arm:<8} {'n/a' if mean is None else f'{mean:.3f}'}")
        lines.extend(f"  note: {n}" for n in self.notes)
        pick = next((q for q in self.queries if q.attempt_id == example), None) or (
            max(self.queries, key=lambda q: len(q.relevant)) if self.queries else None
        )
        if pick:
            lines.append(
                f"\nTop {K} for query {pick.attempt_id} (section {pick.section!r}); "
                f"relevant = {sorted(pick.relevant)}"
            )
            for arm in self.arms:
                if arm in pick.ranked:
                    marked = [f"{i}{'*' if i in pick.relevant else ''}" for i in pick.ranked[arm]]
                    lines.append(f"  {arm:<8} {marked}  recall {pick.recall[arm]:.2f}")
            lines.append("  (* = relevant)")
        return "\n".join(lines)


def _earlier_top_k(results: Iterable[Mapping[str, Any]], allowed: set[str]) -> list[str]:
    ranked: list[str] = []
    for r in results:
        aid = r.get("attempt_id")
        if aid in allowed and aid not in ranked:
            ranked.append(aid)
        if len(ranked) == K:
            break
    return ranked


def run_eval(
    session_id: str,
    *,
    db: Database | None = None,
    embedder: Embedder | None = None,
    arms: Sequence[str] = ARMS,
    auto_min_interval: float = 0.0,
    auto_retries: int = 6,
    sleep: Callable[[float], None] = time.sleep,
    wait_timeout: float = 900,
) -> Report:
    """Run the eval for one session. Call prepare_auto_arm() first if "auto" is in arms."""
    unknown = set(arms) - set(ARMS)
    if unknown:
        raise ValueError(f"unknown arms {sorted(unknown)}; choose from {ARMS}")
    db = db if db is not None else get_db()
    emb = embedder or get_embedder()
    attempts = load_attempts(db, session_id)
    section_of = section_resolver(db)
    report = Report(session_id=session_id, arms=tuple(arms))

    for query in attempts:
        relevant = relevant_ids(query, attempts, section_of)
        if not relevant:
            report.skipped_no_relevant += 1
            continue
        allowed = earlier_ids(query, attempts)
        text = query["intent"]
        ranked: dict[str, list[str]] = {}
        needs_vector = {"vector", "hybrid"} & set(arms)
        qvec = emb.embed_query_cached(text) if needs_vector else None
        for arm in arms:
            if arm == "vector":
                pipeline = vector_pipeline(session_id, qvec)
                results = db["attempts"].aggregate(pipeline)
            elif arm == "keyword":
                results = db["attempts"].aggregate(keyword_pipeline(session_id, text))
            elif arm == "hybrid":
                results = db["attempts"].aggregate(hybrid_pipeline(session_id, text, qvec))
            else:
                results = _auto_query(db, session_id, text, auto_min_interval, auto_retries, sleep)
            ranked[arm] = _earlier_top_k(results, allowed)
        report.queries.append(
            QueryResult(
                attempt_id=query["attempt_id"],
                section=section_of(query["goal_id"]),
                relevant=relevant,
                ranked=ranked,
                recall={arm: recall_at_k(ranked[arm], relevant) for arm in arms},
            )
        )
    return report


_last_auto_query = [0.0]


def _auto_query(
    db: Database,
    session_id: str,
    text: str,
    min_interval: float,
    retries: int,
    sleep: Callable[[float], None],
) -> list[dict[str, Any]]:
    """One Automated Embedding query, paced and retried: M0 without billing allows 3/min."""
    for attempt in range(retries + 1):
        wait = _last_auto_query[0] + min_interval - time.monotonic()
        if wait > 0:
            sleep(wait)
        _last_auto_query[0] = time.monotonic()
        try:
            return list(db[ATTEMPTS_AUTO].aggregate(auto_pipeline(session_id, text)))
        except OperationFailure as err:
            if attempt == retries or not _transient(err):
                raise
            sleep(min(2**attempt * 10, 120))
    raise AssertionError("unreachable")


def _transient(err: OperationFailure) -> bool:
    """Rate limits and the embedding service's 429/5xx responses are worth retrying."""
    message = str(err).lower()
    return "rate" in message or any(
        f"status code: {c}" in message for c in (429, 500, 502, 503, 504)
    )


def prepare_auto_arm(
    session_id: str,
    *,
    db: Database | None = None,
    timeout: float = 1800,
    sleep: Callable[[float], None] = time.sleep,
) -> list[str]:
    """Copy the session to ATTEMPTS_AUTO, build its index, and wait for the initial sync.

    Returns notes for the report. Raises if the model can't match embed() or the sync doesn't
    finish, because a comparison against a different model or a half-built index is meaningless.
    """
    db = db if db is not None else get_db()
    if DIMENSION != AUTO_MODEL_DEFAULT_DIMENSION:
        raise RuntimeError(
            f"embed() uses {DIMENSION} dimensions but Automated Embedding can only produce "
            f"{MODEL}'s default ({AUTO_MODEL_DEFAULT_DIMENSION}); the auto arm would compare "
            "different embeddings"
        )
    attempts = load_attempts(db, session_id)
    count = sync_auto_copy(db, session_id, attempts)
    action = ensure_auto_index(db)
    live_model = auto_index_model(db)
    if live_model != MODEL:
        raise RuntimeError(f"auto index uses {live_model!r}, embed() uses {MODEL!r}")

    def probe() -> int:
        return len(
            list(
                db[ATTEMPTS_AUTO].aggregate(
                    [
                        {
                            "$vectorSearch": {
                                "index": AUTO_INDEX,
                                "path": AUTO_TEXT_FIELD,
                                "query": "commonmark",
                                "numCandidates": max(count, 1),
                                "limit": max(count, 1),
                                "filter": {"session_id": session_id},
                            }
                        },
                        {"$project": {"_id": 1}},
                    ]
                )
            )
        )

    synced = wait_until_searchable(
        probe,
        count,
        ready=_index_ready(db, ATTEMPTS_AUTO, AUTO_INDEX),
        timeout=timeout,
        sleep=sleep,
    )
    if not synced:
        raise TimeoutError(f"Automated Embedding sync didn't finish within {timeout:.0f}s")
    return [
        f"auto index {action}; model {live_model} (embed() uses {MODEL}), "
        f"dimension {AUTO_MODEL_DEFAULT_DIMENSION} (voyage default; embed() pins {DIMENSION}); "
        f"all {count} copies searchable"
    ]


def prepare_direct_arms(
    session_id: str,
    *,
    db: Database | None = None,
    timeout: float = 600,
    sleep: Callable[[float], None] = time.sleep,
) -> list[str]:
    """Wait until attempts_vector and attempts_text can see every attempt of the session.

    Both indexes update asynchronously, so a freshly written attempt may be missing for a moment.
    Attempts without an embedding (Voyage failed, awaiting backfill) can never be found by the
    vector arm; that's reported, not hidden.
    """
    db = db if db is not None else get_db()
    ids = [a["_id"] for a in load_attempts(db, session_id)]
    embedded = db["attempts"].count_documents({"_id": {"$in": ids}, "embedding": {"$exists": True}})
    notes = []
    if embedded < len(ids):
        notes.append(
            f"{len(ids) - embedded} of {len(ids)} attempts have no embedding yet (run "
            "backfill_embeddings()); the vector and hybrid arms can't find them"
        )

    def vector_count() -> int:
        pipeline = [
            {
                "$vectorSearch": {
                    "index": "attempts_vector",
                    "path": "embedding",
                    "queryVector": [1.0] + [0.0] * (DIMENSION - 1),
                    "numCandidates": max(embedded, 1),
                    "limit": max(embedded, 1),
                    "filter": {"session_id": session_id},
                }
            },
            {"$match": {"_id": {"$in": ids}}},
        ]
        return len(list(db["attempts"].aggregate(pipeline)))

    def text_count() -> int:
        pipeline = [
            {"$search": {"index": "attempts_text", "exists": {"path": "intent"}}},
            {"$match": {"_id": {"$in": ids}}},
        ]
        return len(list(db["attempts"].aggregate(pipeline)))

    for name, count, expected in (
        ("attempts_vector", vector_count, embedded),
        ("attempts_text", text_count, len(ids)),
    ):
        if not wait_until_searchable(
            count, expected, ready=_index_ready(db, "attempts", name), timeout=timeout, sleep=sleep
        ):
            raise TimeoutError(f"{name} didn't index all {expected} attempts within {timeout:.0f}s")
    return notes


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("session_id")
    parser.add_argument("--arms", default=",".join(ARMS), help=f"comma-separated, from {ARMS}")
    parser.add_argument("--example", help="attempt id to show side by side")
    parser.add_argument(
        "--auto-interval", type=float, default=0.0, help="seconds between auto queries (21 on M0)"
    )
    args = parser.parse_args(argv)
    arms = tuple(a.strip() for a in args.arms.split(",") if a.strip())
    notes = prepare_direct_arms(args.session_id)
    if "auto" in arms:
        notes += prepare_auto_arm(args.session_id)
    report = run_eval(args.session_id, arms=arms, auto_min_interval=args.auto_interval)
    report.notes.extend(notes)
    print(report.render(example=args.example))
    return 0


if __name__ == "__main__":
    sys.exit(main())
