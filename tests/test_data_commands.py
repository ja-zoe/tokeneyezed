"""`tokeneyezed db ...` and `eval retrieval`. No network: fake collections, fake embedder."""

from types import SimpleNamespace

import pytest
from pymongo.errors import OperationFailure

from tokeneyezed.controller.cli import main
from tokeneyezed.data import commands
from tokeneyezed.data.commands import (
    CHECK_COLLECTION,
    CHECK_INDEX,
    PLAIN_COLLECTIONS,
    REGULAR_INDEXES,
    check_embeddings,
    init_database,
)
from tokeneyezed.data.indexes import INDEXES


class FakeColl:
    def __init__(self, search_indexes=None, hits=None, fail_queries=0):
        self.search = search_indexes or []
        self.regular: list = []
        self.docs: list = []
        self.hits, self.fail_queries = hits, fail_queries

    def list_search_indexes(self):
        return self.search

    def create_search_index(self, model):
        self.search.append({"name": model.document["name"], "latestDefinition": {}})

    def update_search_index(self, name, definition):
        pass

    def create_index(self, keys, **options):
        name = "_".join(f"{k}_{d}" for k, d in keys)
        if name not in self.regular:
            self.regular.append(name)
        return name

    def insert_one(self, doc):
        self.docs.append(doc)
        return SimpleNamespace(inserted_id=len(self.docs))

    def delete_one(self, query):
        self.docs = [d for i, d in enumerate(self.docs, 1) if i != query["_id"]]

    def aggregate(self, pipeline):
        if self.fail_queries > 0:
            self.fail_queries -= 1
            raise OperationFailure("index not ready")
        if self.hits is not None:
            return self.hits
        return [{"_id": i, "score": 0.99} for i, _ in enumerate(self.docs, 1)]


class FakeDb(dict):
    def __missing__(self, name):
        self[name] = FakeColl()
        return self[name]

    def list_collection_names(self):
        return list(self)

    def create_collection(self, name):
        self[name] = FakeColl()


def ready_db(**kw) -> FakeDb:
    db = FakeDb()
    db[CHECK_COLLECTION] = FakeColl(
        search_indexes=[{"name": CHECK_INDEX, "status": "READY", "queryable": True}], **kw
    )
    return db


def test_init_creates_everything_and_is_safe_to_rerun() -> None:
    db = FakeDb()
    first = init_database(db)
    assert all(name in db for name in (*PLAIN_COLLECTIONS, *INDEXES))
    assert {n for idx in INDEXES.values() for n in idx} <= {
        line.split()[1] for line in first if line.startswith("search")
    }
    assert all(c.regular for name, c in db.items() if name in REGULAR_INDEXES)
    snapshot = {name: list(c.regular) for name, c in db.items()}
    second = init_database(db)
    assert {name: list(c.regular) for name, c in db.items()} == snapshot
    assert not any("created" in line for line in second if line.startswith("collection"))


def test_goals_are_unique_by_goal_id() -> None:
    specs = REGULAR_INDEXES["goals"]
    assert any(keys == [("goal_id", 1)] and opts.get("unique") for keys, opts in specs)


def test_check_finds_the_document_and_cleans_up() -> None:
    db = ready_db()
    message = check_embeddings(db, lambda text: [0.1] * 8, sleep=lambda s: None)
    assert message.startswith("ok: embedded 8 dimensions")
    assert db[CHECK_COLLECTION].docs == []


def test_check_waits_out_a_query_that_is_not_ready_yet() -> None:
    db = ready_db(fail_queries=2)
    assert check_embeddings(db, lambda t: [0.1], sleep=lambda s: None).startswith("ok")


def test_check_fails_and_still_cleans_up_when_the_document_never_returns(monkeypatch) -> None:
    db = ready_db(hits=[])
    clock = iter(range(0, 10_000, 50))
    monkeypatch.setattr(commands.time, "monotonic", lambda: next(clock))
    with pytest.raises(RuntimeError, match="never came back"):
        check_embeddings(db, lambda t: [0.1], timeout=100, sleep=lambda s: None)
    assert db[CHECK_COLLECTION].docs == []


@pytest.mark.parametrize("indexes", [[], [{"name": CHECK_INDEX, "status": "PENDING"}]])
def test_check_says_to_run_init_when_the_index_is_not_ready(indexes) -> None:
    db = FakeDb()
    db[CHECK_COLLECTION] = FakeColl(search_indexes=indexes)
    with pytest.raises(RuntimeError, match="db init"):
        check_embeddings(db, lambda t: [0.1])
    assert db[CHECK_COLLECTION].docs == []  # nothing inserted


def test_check_does_not_embed_before_the_index_is_ready() -> None:
    calls = []
    with pytest.raises(RuntimeError):
        check_embeddings(FakeDb(), lambda t: calls.append(t) or [0.1])
    assert calls == []  # no Voyage spend on a setup problem


def test_cli_db_init_and_check(monkeypatch, capsys) -> None:
    db = ready_db()
    monkeypatch.setattr(commands, "get_db", lambda: db)
    assert main(["db", "init"]) == 0
    assert "search      skills_vector" in capsys.readouterr().out

    monkeypatch.setattr(
        commands, "get_embedder", lambda: SimpleNamespace(embed_query_cached=lambda t: [0.1])
    )
    assert main(["db", "check", "--timeout", "1"]) == 0
    assert "ok: embedded" in capsys.readouterr().out


def test_cli_db_check_exits_nonzero_with_the_reason(monkeypatch, capsys) -> None:
    monkeypatch.setattr(commands, "get_db", lambda: FakeDb())
    monkeypatch.setattr(
        commands, "get_embedder", lambda: SimpleNamespace(embed_query_cached=lambda t: [0.1])
    )
    assert main(["db", "check"]) == 1
    assert "FAILED" in capsys.readouterr().err


def test_cli_db_backfill(monkeypatch, capsys) -> None:
    seen = {}
    monkeypatch.setattr(
        commands, "backfill_embeddings", lambda limit: seen.setdefault("n", limit) or 3
    )
    assert main(["db", "backfill", "--limit", "7"]) == 0
    assert seen["n"] == 7
    assert "embedded 7 attempts" in capsys.readouterr().out


def test_cli_eval_retrieval_passes_its_arguments_through(monkeypatch) -> None:
    import tokeneyezed.data.retrieval_eval as retrieval

    seen = {}
    monkeypatch.setattr(retrieval, "main", lambda argv: seen.setdefault("argv", argv) and 0)
    assert main(["eval", "retrieval", "H-1", "--arms", "vector,keyword", "--example", "a-1"]) == 0
    assert seen["argv"] == [
        "H-1",
        "--arms",
        "vector,keyword",
        "--auto-interval",
        "0.0",
        "--example",
        "a-1",
    ]
