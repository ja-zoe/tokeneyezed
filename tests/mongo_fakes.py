"""In-memory stand-ins for pymongo and Voyage, shared by the data and contract tests."""

import urllib.error
from types import SimpleNamespace
from typing import Any

from tokeneyezed.data.embeddings import DIMENSION, Embedder, RateLimiter, TokenBudget


class FakeResult:
    def __init__(self, inserted_id: int) -> None:
        self.inserted_id = inserted_id


class FakeCursor(list):
    def limit(self, n: int) -> "FakeCursor":
        return FakeCursor(self[:n])

    def sort(self, key: str, direction: int) -> "FakeCursor":
        return FakeCursor(sorted(self, key=lambda d: d[key], reverse=direction < 0))


class FakeCollection:
    def __init__(self) -> None:
        self.docs: list[dict[str, Any]] = []

    def insert_one(self, doc: dict[str, Any]) -> FakeResult:
        doc = {**doc, "_id": len(self.docs) + 1}
        self.docs.append(doc)
        return FakeResult(doc["_id"])

    def find(self, query: dict[str, Any], projection: Any = None) -> FakeCursor:
        def matches(doc):
            for key, value in query.items():
                if isinstance(value, dict):
                    if "$ne" in value and doc.get(key) == value["$ne"]:
                        return False
                    if "$exists" in value and (key in doc) != value["$exists"]:
                        return False
                    if "$in" in value and doc.get(key) not in value["$in"]:
                        return False
                    if "$nin" in value and doc.get(key) in value["$nin"]:
                        return False
                elif doc.get(key) != value:
                    return False
            return True

        return FakeCursor(d for d in self.docs if matches(d))

    def find_one(self, query: dict[str, Any], projection: Any = None, sort: Any = None):
        docs = list(self.find(query))
        for key, direction in reversed(sort or []):  # stable sorts, last key first
            present = [d for d in docs if _get(d, key) is not None]
            missing = [d for d in docs if _get(d, key) is None]  # null sorts lowest, as in Mongo
            present.sort(key=lambda d: _get(d, key), reverse=direction < 0)
            docs = missing + present if direction > 0 else present + missing
        return next(iter(docs), None)

    def aggregate(self, pipeline: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return []  # search stages need a real Atlas cluster

    def update_one(self, query: dict[str, Any], update: dict[str, Any], upsert: bool = False):
        for doc in self.find(query):
            doc.update(update.get("$set", {}))
            for key, by in update.get("$inc", {}).items():
                doc[key] = doc.get(key, 0) + by
            for key in update.get("$unset", {}):
                doc.pop(key, None)
            return SimpleNamespace(matched_count=1, modified_count=1, upserted_id=None)
        if upsert:
            doc = {k: v for k, v in query.items() if not isinstance(v, dict)}
            doc.update(update.get("$setOnInsert", {}))
            doc.update(update.get("$set", {}))
            inserted = self.insert_one(doc).inserted_id
            return SimpleNamespace(matched_count=0, modified_count=0, upserted_id=inserted)
        return SimpleNamespace(matched_count=0, modified_count=0, upserted_id=None)


def _get(doc: dict[str, Any], dotted: str) -> Any:
    for part in dotted.split("."):
        if not isinstance(doc, dict):
            return None
        doc = doc.get(part)
    return doc


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
