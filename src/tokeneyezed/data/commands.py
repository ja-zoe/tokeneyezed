"""The data workstream's operator commands: `tokeneyezed db init|check|backfill` and
`tokeneyezed eval retrieval`. controller/cli.py calls `register(subparsers)` (docs/commands.md).

- `db init`      collections, regular indexes, and the Atlas Search / Vector Search indexes.
                 Safe to re-run: existing indexes are left alone, drifted ones are updated.
- `db check`     the embeddings check: embed a test document, insert it, get it back from a
                 vector query, and remove it again. Needs `db init` to have run first.
- `db backfill`  embed attempts, memory summaries, and skills written while Voyage was down.
- `eval retrieval SESSION`  recall@5 across the four retrieval arms (data/retrieval_eval.py).

Every command reads MONGODB_URI (and VOYAGE_API_KEY where it embeds) from the environment, which
controller/cli.py loads from .env.
"""

from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Callable
from typing import Any

from pymongo import ASCENDING, DESCENDING
from pymongo.database import Database
from pymongo.errors import OperationFailure

from tokeneyezed.data.compactor import backfill_memory_embeddings
from tokeneyezed.data.db import get_db
from tokeneyezed.data.embeddings import get_embedder
from tokeneyezed.data.indexes import INDEXES, ensure_search_indexes
from tokeneyezed.data.skills import backfill_skill_embeddings
from tokeneyezed.data.writes import backfill_embeddings

# Collections that need no search index but must exist (docs/master-plan.md, "MongoDB data model").
# The LangGraph saver creates its own, and the eval package creates the held-out results collection
# (invariant I3: nothing outside eval/ may even name it).
PLAIN_COLLECTIONS = ("sessions", "events", "goals", "attempts")

# collection -> [(keys, options)]: the regular indexes from the data model.
REGULAR_INDEXES: dict[str, list[tuple[list[tuple[str, int]], dict[str, Any]]]] = {
    "sessions": [([("session_id", ASCENDING)], {"unique": True})],
    "events": [([("session_id", ASCENDING), ("ts", ASCENDING)], {})],
    # One goal per section per session; `seed` upserts on goal_id and relies on this.
    "goals": [
        ([("goal_id", ASCENDING)], {"unique": True}),
        ([("session_id", ASCENDING), ("status", ASCENDING), ("priority", ASCENDING)], {}),
    ],
    "attempts": [
        ([("attempt_id", ASCENDING)], {}),
        ([("session_id", ASCENDING), ("goal_id", ASCENDING), ("number", DESCENDING)], {}),
    ],
}

# IndexOptionsConflict, IndexKeySpecsConflict: an existing index has the same keys, other options.
INDEX_CONFLICT_CODES = (85, 86)
CONFLICT = "CONFLICT   "

CHECK_COLLECTION = "skills"  # has a vector index and no filter fields, so a bare test doc fits
CHECK_INDEX = "skills_vector"
CHECK_TAG = "_dbcheck"
CHECK_TEXT = "tokeneyezed db check: a document to embed, store, and find again"


def init_database(db: Database) -> list[str]:
    """Create collections and indexes. Returns one line per thing done, for printing."""
    lines: list[str] = []
    existing = set(db.list_collection_names())
    for name in PLAIN_COLLECTIONS:
        if name not in existing:
            db.create_collection(name)
            lines.append(f"collection  {name}  created")
    for collection, specs in REGULAR_INDEXES.items():
        for keys, options in specs:
            try:
                index_name = db[collection].create_index(keys, **options)  # no-op if it exists
            except OperationFailure as err:
                if err.code not in INDEX_CONFLICT_CODES:
                    raise
                # An index on the same keys exists with other options (e.g. not unique). MongoDB
                # won't change it in place: report it and carry on with the rest.
                lines.append(
                    f"{CONFLICT}  {collection} {dict(keys)} {options}: an existing index differs; "
                    "drop it and re-run to apply"
                )
                continue
            lines.append(f"index       {collection}.{index_name}")
    for name, action in ensure_search_indexes(db).items():
        lines.append(f"search      {name}  {action}")
    return lines


def _index_state(db: Database) -> tuple[bool, str]:
    for index in db[CHECK_COLLECTION].list_search_indexes():
        if index["name"] == CHECK_INDEX:
            if index.get("status") == "READY" and index.get("queryable"):
                return True, "READY"
            return False, str(index.get("status"))
    return False, "missing"


def check_embeddings(
    db: Database,
    embed: Callable[[str], list[float]],
    *,
    timeout: float = 120,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    """Embed a test document, insert it, find it again by vector query, then delete it.

    Returns a one-line success message; raises RuntimeError saying which step failed.
    """
    ready, state = _index_state(db)
    if not ready:
        raise RuntimeError(
            f"{CHECK_INDEX} is {state}; run `tokeneyezed db init` and wait for it to be READY"
        )
    vector = embed(CHECK_TEXT)
    coll = db[CHECK_COLLECTION]
    inserted = coll.insert_one({CHECK_TAG: True, "description": CHECK_TEXT, "embedding": vector})
    try:
        pipeline = [
            {
                "$vectorSearch": {
                    "index": CHECK_INDEX,
                    "path": "embedding",
                    "queryVector": vector,
                    "numCandidates": 50,
                    "limit": 5,
                }
            },
            {"$project": {CHECK_TAG: 1, "score": {"$meta": "vectorSearchScore"}}},
        ]
        deadline = time.monotonic() + timeout
        while True:
            try:
                hits = [h for h in coll.aggregate(pipeline) if h["_id"] == inserted.inserted_id]
            except OperationFailure:
                hits = []
            if hits:
                return (
                    f"ok: embedded {len(vector)} dimensions, stored, and found again "
                    f"(score {hits[0]['score']:.3f})"
                )
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    f"the test document was inserted but never came back from {CHECK_INDEX} "
                    f"within {timeout:.0f}s: check the index dimensions and that it is queryable"
                )
            sleep(3)
    finally:
        coll.delete_one({"_id": inserted.inserted_id})


def cmd_db_init(args: argparse.Namespace) -> int:
    lines = init_database(get_db())
    for line in lines:
        print(line)
    print(f"done: {sum(len(v) for v in INDEXES.values())} search indexes build in Atlas; wait for")
    print("READY there before querying them.")
    conflicts = [line for line in lines if line.startswith(CONFLICT)]
    if conflicts:
        print(f"{len(conflicts)} index conflict(s) above were not applied.", file=sys.stderr)
        return 1
    return 0


def cmd_db_check(args: argparse.Namespace) -> int:
    try:
        print(check_embeddings(get_db(), get_embedder().embed_query_cached, timeout=args.timeout))
    except RuntimeError as err:
        print(f"FAILED: {err}", file=sys.stderr)
        return 1
    return 0


def cmd_db_backfill(args: argparse.Namespace) -> int:
    fixed = backfill_embeddings(limit=args.limit)
    print(f"embedded {fixed} attempts that were written without a vector")
    memories = backfill_memory_embeddings(limit=args.limit)
    print(f"embedded {memories} memory summaries that were written without a vector")
    skills = backfill_skill_embeddings(limit=args.limit)
    print(f"embedded {skills} skills that were written without a vector")
    return 0


def cmd_eval_retrieval(args: argparse.Namespace) -> int:
    from tokeneyezed.data.retrieval_eval import main as retrieval_main

    argv = [args.session_id, "--arms", args.arms, "--auto-interval", str(args.auto_interval)]
    if args.example:
        argv += ["--example", args.example]
    return retrieval_main(argv)


def register(subparsers: Any) -> None:
    db = subparsers.add_parser("db", help="database setup and maintenance").add_subparsers(
        dest="db_command", required=True
    )
    init = db.add_parser("init", help="create collections and indexes (safe to re-run)")
    init.set_defaults(func=cmd_db_init)

    check = db.add_parser("check", help="embed, store, and query back one test document")
    check.add_argument("--timeout", type=float, default=120, help="seconds to wait for the query")
    check.set_defaults(func=cmd_db_check)

    backfill = db.add_parser("backfill", help="embed documents written while Voyage was down")
    backfill.add_argument("--limit", type=int, default=100)
    backfill.set_defaults(func=cmd_db_backfill)

    ev = subparsers.add_parser("eval", help="data evals").add_subparsers(
        dest="eval_command", required=True
    )
    retrieval = ev.add_parser("retrieval", help="recall@5 across four retrieval arms")
    retrieval.add_argument("session_id")
    retrieval.add_argument("--arms", default="vector,keyword,hybrid,auto")
    retrieval.add_argument("--example", help="attempt id to show side by side")
    retrieval.add_argument("--auto-interval", type=float, default=0.0)
    retrieval.set_defaults(func=cmd_eval_retrieval)
