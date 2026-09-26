"""Dashboard reads and routes, on a small fixture database built here (the app never serves it)."""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from tokeneyezed.dashboard.server import App
from tokeneyezed.data import dashboard_reads as reads
from tokeneyezed.eval.held_out import COLLECTION

T0 = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
B, HMEM, H, LIVE = "B-0926-113000", "H-mem-0926-133000", "H-0926-133500", "H-0926-171000"


class Coll:
    def __init__(self) -> None:
        self.docs: list[dict[str, Any]] = []

    def find(self, query: dict[str, Any] | None = None, projection: Any = None) -> list:
        query = query or {}
        return [d for d in self.docs if all(d.get(k) == v for k, v in query.items())]

    def distinct(self, key: str) -> list:
        return list(dict.fromkeys(d[key] for d in self.docs if key in d))


class FakeDb(dict):
    def __missing__(self, name: str) -> Coll:
        self[name] = Coll()
        return self[name]


def add_run(
    db, sid, start, vals, *, agents=None, flagged=(), running=False, block_at=None, usage=None
):
    """One closed attempt per value in `vals` (the last is left running when `running`)."""
    for i, val in enumerate(vals, 1):
        aid, created = f"{sid}-{i:03d}", start + timedelta(minutes=10 * i)
        live = running and i == len(vals)
        db["attempts"].docs.append(
            {
                "attempt_id": aid,
                "number": i,
                "session_id": sid,
                "goal_id": f"{sid}:Tabs",
                "agent": (agents or {}).get(i, "claude"),
                "intent": f"attempt {i}",
                "status": "running" if live else "closed",
                "diff_summary": "" if live else "tabs.py",
                "visible_pass": None if live else val + 0.1,
                "val_pass": None if live else val,
                "outcome": None if live else "flagged" if i in flagged else "improved",
                "observer_flags": ["validation fell"] if i in flagged else [],
                "usage": None if live or usage is None else usage(i),
                "created_at": created,
                "closed_at": None if live else created + timedelta(minutes=7),
            }
        )
        if not live:
            db[COLLECTION].docs.append(
                {"session_id": sid, "attempt_id": aid, "test_pass": max(0.0, val - 0.05)}
            )
        db["events"].docs.append(
            {
                "session_id": sid,
                "attempt_id": aid,
                "agent": "claude",
                "phase": "pre",
                "tool": "bash",
                "input": "pip install markdown-it-py" if i == block_at else "pytest -q",
                "verdict": "block: honeypot" if i == block_at else "allow",
                "ts": created + timedelta(minutes=1),
            }
        )
    db["goals"].docs.append(
        {
            "goal_id": f"{sid}:Tabs",
            "session_id": sid,
            "section": "Tabs",
            "status": "open" if running else "complete",
            "priority": 0,
            "completion_criteria": {"val_pass": 0.85},
        }
    )


@pytest.fixture
def db():
    db = FakeDb()
    add_run(db, B, T0, [0.2, 0.3, 0.3, 0.4, 0.5])  # no usage recorded
    add_run(db, HMEM, T0 + timedelta(hours=1), [0.3, 0.4, 0.5, 0.6])
    add_run(
        db,
        H,
        T0 + timedelta(hours=1, minutes=5),
        [0.3, 0.5, 0.9, 0.6, 0.7, 0.8],
        agents={4: "codex", 5: "codex", 6: "codex"},
        flagged=(3,),
        block_at=2,
        usage=lambda i: {"peak_context": 100_000 * i, "tokens_processed": 1_000_000 * i},
    )
    add_run(db, LIVE, T0 + timedelta(hours=4), [0.3, 0.5, 0.0], running=True)
    return db


def test_an_empty_database_shows_nothing(tmp_path):
    empty = FakeDb()
    assert reads.list_runs(empty) == []
    assert reads.default_compare_ids(empty) == []
    assert reads.running_session(empty) is None
    assert reads.run_detail("anything", empty) is None
    assert reads.compare_runs([], empty) == {"runs": [], "common": 0}
    app = App(empty)
    assert app.api("/api/runs", {}) == (200, [])
    assert app.api("/api/live", {}) == (200, {"session_id": None})


def test_run_name_from_config_then_from_session_id():
    assert reads.run_name("whatever", {"config": {"name": "H-mem"}}) == "H-mem"
    assert reads.run_name("H-mem-0926-1") == "H-mem"
    assert reads.run_name("B-0926-1") == "B"
    assert reads.run_name("h-0926-1") == "H"
    assert reads.run_name("attempt-0926") == "other"


def test_list_runs_covers_every_config_newest_first(db):
    runs = reads.list_runs(db)
    assert {r["name"] for r in runs} == {"B", "H", "H-mem"}
    stamps = [r["updated_at"] for r in runs]
    assert stamps == sorted(stamps, reverse=True)
    assert [r["status"] for r in runs].count("running") == 1


def test_held_out_never_appears_inside_attempt_fields(db):
    """The held-out score comes from test_evals only; attempts carry validation and visible."""
    detail = reads.run_detail(H, db)
    sample = db["attempts"].find({"session_id": H})[0]
    assert "test_pass" not in sample and "held_out" not in sample
    assert all(p["held_out"] is not None for p in detail["attempts"])


def test_flagged_attempts_do_not_count_toward_progress(db):
    """I8: a flagged attempt stays visible in the series but never raises the running best."""
    detail = reads.run_detail(H, db)
    flagged = [p for p in detail["attempts"] if p["outcome"] == "flagged"]
    assert flagged and not flagged[0]["clean"]
    before = detail["attempts"][flagged[0]["number"] - 2]
    assert flagged[0]["best_val"] == before["best_val"]
    assert detail["run"]["flagged"] == 1


def test_handoff_is_where_the_agent_changes(db):
    detail = reads.run_detail(H, db)
    assert detail["handoffs"] == [{"number": 4, "from": "claude", "to": "codex"}]
    assert reads.run_detail(B, db)["handoffs"] == []


def test_blocked_events_come_from_the_event_log(db):
    blocked = reads.run_detail(H, db)["blocked"]
    assert [b["verdict"] for b in blocked] == ["block: honeypot"]
    assert "markdown-it-py" in blocked[0]["input"]


def test_unknown_run_is_none(db):
    assert reads.run_detail("nope", db) is None
    assert reads.run_feed("nope", db=db)["known"] is False


def test_compare_caps_at_the_shortest_run(db):
    ids = [r["session_id"] for r in reads.list_runs(db)]
    result = reads.compare_runs(ids, db)
    assert result["common"] == min(len(r["points"]) for r in result["runs"])


def test_default_compare_is_newest_of_each_config_in_order(db):
    ids = reads.default_compare_ids(db)
    assert [reads.run_name(i) for i in ids] == ["B", "H", "H-mem"]
    newest_h = next(r for r in reads.list_runs(db) if r["name"] == "H")
    assert newest_h["session_id"] in ids


def test_feed_is_ordered_and_resumes_from_a_cursor(db):
    first = reads.run_feed(H, db=db)
    stamps = [e["ts"] for e in first["entries"]]
    assert stamps == sorted(stamps) and not first["running"]
    assert any(e["kind"] == "bad" and "BLOCKED" in e["text"] for e in first["entries"])
    tail = reads.run_feed(H, after=first["cursor"], db=db)
    assert [e["ts"] for e in tail["entries"]] == [first["cursor"]]  # inclusive, client de-dupes


def test_feed_reports_running(db):
    assert reads.run_feed(LIVE, db=db)["running"] is True
    assert reads.running_session(db) == LIVE


def test_api_routes(db):
    app = App(db)
    assert app.api("/api/meta", {})[0] == 404  # nothing but database content is served
    assert app.api("/api/runs/H-0926-133500", {})[0] == 200
    assert app.api("/api/runs/H-0926-133500/feed", {})[0] == 200
    assert app.api("/api/runs/missing", {})[0] == 404
    assert app.api("/api/live", {})[1] == {"session_id": LIVE}
    assert app.api("/api/nope", {})[0] == 404
    status, body = app.api("/api/compare", {"ids": [f"{B},{H}"]})
    assert status == 200 and [r["name"] for r in body["runs"]] == ["B", "H"]


def test_token_usage_is_the_headline_metric_and_is_never_invented(db):
    """Runs with usage sum it up; runs without report None, not zero."""
    summary = {r["session_id"]: r for r in reads.list_runs(db)}
    assert summary[H]["tokens_processed"] == 21_000_000  # 1M + 2M + ... + 6M
    assert summary[H]["peak_context"] == 600_000
    assert summary[H]["usage_attempts"] == 6
    assert summary[B]["tokens_processed"] is None and summary[B]["peak_context"] is None
    assert summary[B]["usage_attempts"] == 0


def test_attempt_and_compare_points_carry_tokens(db):
    points = reads.run_detail(H, db)["attempts"]
    assert [p["tokens_processed"] for p in points][:2] == [1_000_000, 2_000_000]
    assert points[0]["context_tokens"] == 100_000
    compared = reads.compare_runs([B, H], db)
    assert compared["runs"][0]["points"][0]["tokens_processed"] is None
    assert compared["runs"][1]["points"][0]["tokens_processed"] == 1_000_000
    running = reads.run_detail(LIVE, db)["attempts"][-1]
    assert running["tokens_processed"] is None  # an attempt in flight has no usage yet
