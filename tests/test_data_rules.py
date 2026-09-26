from datetime import UTC, datetime, timedelta

import pytest

from tokeneyezed.data.commands import PLAIN_COLLECTIONS, REGULAR_INDEXES
from tokeneyezed.data.rules import (
    load_active_rules,
    load_learning_events,
    load_rules,
    store_learned_rules,
)


class Cursor(list):
    def sort(self, key, direction):
        super().sort(key=lambda item: item[key], reverse=direction < 0)
        return self

    def limit(self, count):
        return Cursor(self[:count])


class Collection:
    def __init__(self, documents=()):
        self.documents = list(documents)

    def find(self, query):
        return Cursor(
            document
            for document in self.documents
            if all(document.get(key) == value for key, value in query.items())
        )

    def update_one(self, identity, update, upsert=False):
        document = next(
            (item for item in self.documents if all(item.get(k) == v for k, v in identity.items())),
            None,
        )
        if document is None:
            if not upsert:
                return
            document = {**identity, **update.get("$setOnInsert", {})}
            self.documents.append(document)
        document.update(update.get("$set", {}))


class Database:
    def __init__(self, **collections):
        self.collections = collections

    def __getitem__(self, name):
        return self.collections.setdefault(name, Collection())


def rule(status="active"):
    return {
        "tool": "bash",
        "check_type": "input_contains",
        "pattern": "git reset --hard",
        "evidence_event_ids": ["event-1", "event-2"],
        "replay": {
            "hits_on_flagged": 2,
            "hits_on_good": 0,
            "hits_on_unlabeled": 0,
            "minimum_support": 2,
        },
        "status": status,
        "version": 1,
    }


def test_load_learning_events_is_bounded_and_chronological():
    now = datetime.now(UTC)
    db = Database(events=Collection([{"ts": now}, {"ts": now + timedelta(seconds=1)}]))

    assert load_learning_events(db=db, limit=1) == [{"ts": now + timedelta(seconds=1)}]
    with pytest.raises(ValueError, match="positive integer"):
        load_learning_events(db=db, limit=0)


def test_rule_results_upsert_by_version_and_load_only_active_rules():
    db = Database(rules=Collection())

    assert store_learned_rules([rule()], db=db) == 1
    assert store_learned_rules([rule("candidate")], db=db) == 1
    assert len(load_rules(db=db)) == 1
    assert load_active_rules(db=db) == []
    assert load_rules(db=db)[0]["status"] == "candidate"


def test_store_rejects_unsupported_rule_shape():
    db = Database(rules=Collection())

    with pytest.raises(ValueError, match="invalid learned rule"):
        store_learned_rules([{**rule(), "check_type": "regex"}], db=db)


def test_rules_collection_has_a_stable_unique_identity_index():
    keys, options = REGULAR_INDEXES["rules"][0]

    assert "rules" in PLAIN_COLLECTIONS
    assert [field for field, _direction in keys] == [
        "tool",
        "check_type",
        "pattern",
        "version",
    ]
    assert options == {"unique": True}
