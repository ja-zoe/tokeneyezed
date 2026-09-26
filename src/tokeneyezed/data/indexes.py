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
