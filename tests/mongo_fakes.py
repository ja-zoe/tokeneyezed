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

    def find(self, query: dict[str, Any]) -> FakeCursor:
        def matches(doc):
            for key, value in query.items():
                if isinstance(value, dict):
                    if "$ne" in value and doc.get(key) == value["$ne"]:
                        return False
                    if "$exists" in value and (key in doc) != value["$exists"]:
                        return False
                    if "$in" in value and doc.get(key) not in value["$in"]:
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
