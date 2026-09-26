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
    # Skills are per session (data/skills.py), so the brief filters on session_id.
    "skills": {"skills_vector": ("vectorSearch", _vector(["session_id"]))},
    # The observer's repeat-failure check and rule learner search observer flags by `check`.
    "interventions": {"interventions_vector": ("vectorSearch", _vector(["check"]))},
}

# Defaults Atlas fills into the definition it stores. Without them every comparison with a live
# index reports drift, and ensure_search_indexes() would rebuild the index on each call.
_SEARCH_DEFAULTS = {"analyzer": "lucene.standard"}
_VECTOR_FIELD_DEFAULTS = {"quantization": "none"}


def normalized(kind: str, definition: dict[str, Any]) -> dict[str, Any]:
    """The definition with Atlas's defaults filled in, so live and code versions compare equal."""
    if kind == "search":
        return {**_SEARCH_DEFAULTS, **definition}
    fields = [
        {**_VECTOR_FIELD_DEFAULTS, **f} if f.get("type") == "vector" else dict(f)
        for f in definition.get("fields", [])
    ]
    return {**definition, "fields": fields}


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
            elif normalized(kind, live[name].get("latestDefinition", {})) != normalized(
                kind, definition
            ):
                db[collection].update_search_index(name, definition)
                actions[name] = "updated"
            else:
                actions[name] = "unchanged"
    return actions
