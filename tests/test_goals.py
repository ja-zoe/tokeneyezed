"""MongoGoalStore, replan notes reaching the brief, and the data_ports() wiring. No network."""

from datetime import datetime

import pytest
from mongo_fakes import FakeDB, embedder

from tokeneyezed.controller import fakes
from tokeneyezed.data.brief import MongoBriefBuilder, goal_text
from tokeneyezed.data.compactor import MongoCompactor
from tokeneyezed.data.goals import COMPLETE, OPEN, MongoGoalStore, goal_id_for
from tokeneyezed.data.ledger import MongoLedger
from tokeneyezed.data.wiring import data_ports
from tokeneyezed.ports import BriefBuilder, Compactor, Goal, GoalStore, Ledger, Ports

SECTIONS = ["Tabs", "Links", "Code spans"]


def seeded() -> tuple[FakeDB, MongoGoalStore]:
    db = FakeDB()
    store = MongoGoalStore(db=db)
    store.seed("S", SECTIONS, 0.85)
    return db, store


def test_seed_writes_one_goal_per_section_in_the_master_plan_shape() -> None:
    db, _ = seeded()
    docs = db["goals"].docs
    assert [d["section"] for d in docs] == SECTIONS
    first = docs[0]
    assert first["goal_id"] == "S:Tabs" == goal_id_for("S", "Tabs")
    assert first["session_id"] == "S" and first["status"] == OPEN and first["priority"] == 0
    assert first["completion_criteria"] == {"val_pass": 0.85}
    assert first["strategy_notes"] is None and first["replan_count"] == 0
    assert first["last_replanned_at"] is None and isinstance(first["created_at"], datetime)


def test_next_open_follows_priority_and_returns_the_shared_goal_type() -> None:
    _, store = seeded()
    goal = store.next_open("S")
    assert goal == Goal(goal_id="S:Tabs", section="Tabs", target_val_pass=0.85)
    store.complete("S:Tabs")
    assert store.next_open("S").section == "Links"


def test_all_goals_complete_means_no_open_goal() -> None:
    db, store = seeded()
    for section in SECTIONS:
        store.complete(goal_id_for("S", section))
    assert store.next_open("S") is None
    assert {d["status"] for d in db["goals"].docs} == {COMPLETE}
    assert all(isinstance(d["completed_at"], datetime) for d in db["goals"].docs)


def test_sessions_are_isolated() -> None:
    _, store = seeded()
    assert store.next_open("other") is None
    store.seed("other", ["Tabs"], 0.9)
    assert store.next_open("other").goal_id == "other:Tabs"
    assert store.next_open("S").goal_id == "S:Tabs"


def test_replan_records_the_strategy_and_keeps_the_goal_open_at_its_priority() -> None:
    db, store = seeded()
    store.replan("S:Tabs", "3 attempts without improvement; best validation 0.40")
    store.replan("S:Tabs", "try expanding tabs to 4-column stops first")
    doc = db["goals"].docs[0]
    assert doc["status"] == OPEN and doc["priority"] == 0
    assert doc["strategy_notes"] == "try expanding tabs to 4-column stops first"
    assert doc["replan_count"] == 2 and isinstance(doc["last_replanned_at"], datetime)
    assert store.next_open("S").goal_id == "S:Tabs"  # the same goal, now with a new strategy
    assert store.strategy_notes("S:Tabs") == "try expanding tabs to 4-column stops first"
    assert store.strategy_notes("S:Links") is None


def test_reseeding_never_resets_progress() -> None:
    db, store = seeded()
    store.complete("S:Tabs")
    store.replan("S:Links", "new plan")
    store.seed("S", SECTIONS, 0.5)
    by_id = {d["goal_id"]: d for d in db["goals"].docs}
    assert len(db["goals"].docs) == 3
    assert by_id["S:Tabs"]["status"] == COMPLETE
    assert by_id["S:Links"]["strategy_notes"] == "new plan"
    assert by_id["S:Links"]["completion_criteria"] == {"val_pass": 0.85}


@pytest.mark.parametrize("call", ["replan", "complete"])
def test_unknown_goal_is_an_error(call: str) -> None:
    _, store = seeded()
    with pytest.raises(LookupError):
        getattr(store, call)(*(["S:Nope", "note"] if call == "replan" else ["S:Nope"]))


@pytest.mark.parametrize(
    ("sections", "target"),
    [
        ([], 0.85),
        ("Tabs", 0.85),
        (["Tabs", "Tabs"], 0.85),
        (["Tabs", ""], 0.85),
        (["Tabs"], 0),
        (["Tabs"], 1.5),
        (["Tabs"], True),
    ],
)
def test_seed_rejects_bad_input(sections, target) -> None:
    with pytest.raises(ValueError):
        MongoGoalStore(db=FakeDB()).seed("S", sections, target)


def test_replan_rejects_an_empty_note() -> None:
    _, store = seeded()
    with pytest.raises(ValueError):
        store.replan("S:Tabs", "")


# --- the replan note reaches the planner through the brief --------------------------------


def test_goal_text_carries_the_strategy_only_after_a_replan() -> None:
    assert goal_text("Tabs", None) == "Tabs"
    assert goal_text("Tabs", "use tab stops") == "Tabs. Replanned; strategy now: use tab stops"


def test_a_replan_note_appears_in_the_next_brief() -> None:
    db, store = seeded()
    builder = MongoBriefBuilder(db=db, embedder=embedder())
    goal = store.next_open("S")
    assert "Replanned" not in builder.build("S", goal, use_memory=True)
    store.replan(goal.goal_id, "expand tabs to 4-column stops before parsing blocks")
    brief = builder.build("S", store.next_open("S"), use_memory=True)
    goal_section = brief.split("## Goal")[1].split("##")[0]
    assert "expand tabs to 4-column stops before parsing blocks" in goal_section


# --- wiring ---------------------------------------------------------------------------------


def test_data_ports_are_the_real_implementations_of_the_four_data_ports() -> None:
    db, emb = FakeDB(), embedder()
    ports = data_ports(db=db, embedder=emb)
    assert set(ports) == {"goals", "brief", "ledger", "compactor"}
    assert isinstance(ports["goals"], MongoGoalStore) and isinstance(ports["goals"], GoalStore)
    assert isinstance(ports["brief"], MongoBriefBuilder) and isinstance(
        ports["brief"], BriefBuilder
    )
    assert isinstance(ports["ledger"], MongoLedger) and isinstance(ports["ledger"], Ledger)
    assert isinstance(ports["compactor"], MongoCompactor)
    assert isinstance(ports["compactor"], Compactor)
    # one database and one Embedder, so the rate limiter, budget and query cache are shared
    for port in ports.values():
        assert port._db is db
    for name in ("brief", "ledger", "compactor"):
        assert ports[name]._embedder is emb


def test_data_ports_plug_into_the_controllers_ports() -> None:
    others = fakes.fake_ports()
    ports = Ports(
        **data_ports(db=FakeDB(), embedder=embedder()),
        planner=others.planner,
        runner=others.runner,
        scorer=others.scorer,
        reviewer=others.reviewer,
    )
    assert isinstance(ports.goals, MongoGoalStore)
