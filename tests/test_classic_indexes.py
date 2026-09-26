"""Regular (B-tree) index definitions and ensure_classic_indexes(). No network."""

from tokeneyezed.data.indexes import CLASSIC_INDEXES, ensure_classic_indexes


class FakeIndexedCollection:
    def __init__(self, live: dict | None = None) -> None:
        self.live = live or {"_id_": {"key": [("_id", 1)]}}
        self.created: list[tuple] = []

    def index_information(self) -> dict:
        return self.live

    def create_index(self, keys, name: str, **options) -> str:
        self.created.append((name, keys, options))
        self.live[name] = {"key": keys, **options}
        return name


class FakeIndexedDB(dict):
    def __missing__(self, name: str) -> FakeIndexedCollection:
        self[name] = FakeIndexedCollection()
        return self[name]


def test_goal_ids_are_unique() -> None:
    keys, options = CLASSIC_INDEXES["goals"]["goal_id_1"]
    assert keys == [("goal_id", 1)] and options == {"unique": True}


def test_the_master_plan_indexes_are_defined_in_code() -> None:
    assert CLASSIC_INDEXES["sessions"]["session_id_1"][0] == [("session_id", 1)]
    assert CLASSIC_INDEXES["events"]["session_id_1_ts_1"][0] == [("session_id", 1), ("ts", 1)]
    assert CLASSIC_INDEXES["goals"]["session_id_1_status_1_priority_1"][0] == [
        ("session_id", 1),
        ("status", 1),
        ("priority", 1),
    ]


def test_ensure_creates_missing_indexes_and_is_idempotent() -> None:
    db = FakeIndexedDB()
    first = ensure_classic_indexes(db)
    assert set(first.values()) == {"created"}
    assert ("goal_id_1", [("goal_id", 1)], {"unique": True}) in db["goals"].created
    second = ensure_classic_indexes(db)
    assert set(second.values()) == {"unchanged"}


def test_a_same_named_index_with_other_options_is_a_conflict_not_silently_kept() -> None:
    db = FakeIndexedDB()
    db["goals"] = FakeIndexedCollection(
        {"_id_": {"key": [("_id", 1)]}, "goal_id_1": {"key": [("goal_id", 1)]}}  # not unique
    )
    assert ensure_classic_indexes(db)["goal_id_1"] == "conflict"
    assert not any(name == "goal_id_1" for name, *_ in db["goals"].created)
