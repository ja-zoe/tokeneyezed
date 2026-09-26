"""Atlas Search and Vector Search index definitions, in code so a rebuilt cluster gets them back.

The brief builder's $vectorSearch `filter` may only use fields declared here as `filter` fields;
Atlas rejects the query otherwise ("Path 'status' needs to be indexed as filter"), and fake
databases in unit tests can't catch that. tests/test_indexes.py ties the two together.
"""

from __future__ import annotations

from typing import Any

from pymongo.database import Database
from pymongo.operations import SearchIndexModel

from tokeneyezed.data.embeddings import DIMENSION


def _vector(filters: list[str]) -> dict[str, Any]:
    return {
        "fields": [
            {
                "type": "vector",
                "path": "embedding",
                "numDimensions": DIMENSION,
                "similarity": "cosine",
            },
            *({"type": "filter", "path": path} for path in filters),
        ]
    }


# collection -> index name -> (type, definition)
INDEXES: dict[str, dict[str, tuple[str, dict[str, Any]]]] = {
    "attempts": {
        "attempts_vector": (
            "vectorSearch",
            _vector(["session_id", "goal_id", "outcome", "status"]),
        ),
        "attempts_text": (
            "search",
            {
                "mappings": {
                    "dynamic": False,
                    "fields": {"intent": {"type": "string"}, "diff_summary": {"type": "string"}},
                }
            },
        ),
    },
    "memory": {"memory_vector": ("vectorSearch", _vector(["session_id"]))},
    "skills": {"skills_vector": ("vectorSearch", _vector([]))},
}


def filter_paths(definition: dict[str, Any]) -> set[str]:
    return {f["path"] for f in definition.get("fields", []) if f.get("type") == "filter"}


def ensure_search_indexes(db: Database) -> dict[str, str]:
    """Create missing indexes and update ones whose definition drifted. Returns name -> action.

    Index builds are asynchronous: wait for status READY in Atlas before querying. Collections
    must exist first (Atlas can't index a missing collection), so this creates them if needed.
    """
    actions: dict[str, str] = {}
    existing = set(db.list_collection_names())
    for collection, indexes in INDEXES.items():
        if collection not in existing:
            db.create_collection(collection)
        live = {i["name"]: i for i in db[collection].list_search_indexes()}
        for name, (kind, definition) in indexes.items():
            if name not in live:
                db[collection].create_search_index(
                    SearchIndexModel(definition=definition, name=name, type=kind)
                )
                actions[name] = "created"
            elif live[name].get("latestDefinition") != definition:
                db[collection].update_search_index(name, definition)
                actions[name] = "updated"
            else:
                actions[name] = "unchanged"
    return actions


# Regular (B-tree) indexes: collection -> index name -> (keys, options). The master plan's
# "MongoDB data model" indexes, plus uniqueness on the ids the data layer looks documents up by.
CLASSIC_INDEXES: dict[str, dict[str, tuple[list[tuple[str, int]], dict[str, Any]]]] = {
    "sessions": {"session_id_1": ([("session_id", 1)], {})},
    "events": {"session_id_1_ts_1": ([("session_id", 1), ("ts", 1)], {})},
    "goals": {
        "session_id_1_status_1_priority_1": (
            [("session_id", 1), ("status", 1), ("priority", 1)],
            {},
        ),
        # seed() upserts by goal_id; the unique index stops two concurrent seeds from ever
        # creating the same goal twice.
        "goal_id_1": ([("goal_id", 1)], {"unique": True}),
    },
}


def ensure_classic_indexes(db: Database) -> dict[str, str]:
    """Create any missing regular index. Returns name -> "created" | "unchanged".

    An existing index with the same name but different keys or options is reported as
    "conflict" and left alone: changing it means dropping it, which is a deliberate step.
    """
    actions: dict[str, str] = {}
    for collection, indexes in CLASSIC_INDEXES.items():
        live = db[collection].index_information()
        for name, (keys, options) in indexes.items():
            if name not in live:
                db[collection].create_index(keys, name=name, **options)
                actions[name] = "created"
            elif live[name]["key"] != keys or any(
                live[name].get(k) != v for k, v in options.items()
            ):
                actions[name] = "conflict"
            else:
                actions[name] = "unchanged"
    return actions
