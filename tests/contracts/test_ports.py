"""Contract tests: every implementation of a port, fake or real, must pass these.

To plug in a real implementation, add a factory to that port's list below. If it needs Atlas or an
API key, wrap it in `pytest.param(..., marks=needs_env("MONGODB_URI"))` so it skips cleanly
without credentials (and CI still runs the fakes).
"""

import json
import os
import tempfile
import uuid
from pathlib import Path

import pytest
from fake_claude import make_runner
from fake_codex import make_codex_runner
from mongo_fakes import FakeDB, embedder

from tokeneyezed.controller import fakes
from tokeneyezed.controller.planner import OpenRouterPlanner
from tokeneyezed.data.brief import MongoBriefBuilder
from tokeneyezed.data.compactor import MongoCompactor
from tokeneyezed.data.goals import MongoGoalStore
from tokeneyezed.data.ledger import MongoLedger
from tokeneyezed.eval.scoring import SpecScorer
from tokeneyezed.observer.reviewer import GamingReviewer
from tokeneyezed.openrouter import OpenRouterClient
from tokeneyezed.ports import (
    AttemptResult,
    AttemptRunner,
    BriefBuilder,
    Compactor,
    Goal,
    GoalStore,
    Ledger,
    Planner,
    Review,
    Reviewer,
    Score,
    Scorer,
)


def needs_env(var: str):
    return pytest.mark.skipif(not os.environ.get(var), reason=f"{var} not set")


# Real data ports against Atlas: each gets a throwaway database, dropped when the session ends.
# They run only when MONGODB_URI is set (CI has no Atlas credentials, so it runs the fakes).
_ATLAS_DB_PREFIX = "tokeneyezed_contract_"
_atlas_dbs: list = []


def atlas_db():
    from pymongo import MongoClient

    db = MongoClient(os.environ["MONGODB_URI"])[f"{_ATLAS_DB_PREFIX}{uuid.uuid4().hex[:10]}"]
    _atlas_dbs.append(db)
    return db


@pytest.fixture(scope="module", autouse=True)
def _drop_atlas_dbs():
    yield
    for db in _atlas_dbs:
        assert db.name.startswith(_ATLAS_DB_PREFIX)  # never drop anything else
        # Drop collection by collection: the project's Atlas user may not hold dropDatabase, and a
        # database with no collections left disappears on its own.
        for name in db.list_collection_names():
            db.drop_collection(name)
    _atlas_dbs.clear()


def on_atlas(factory):
    return pytest.param(factory, marks=needs_env("MONGODB_URI"), id=f"atlas-{factory.__name__}")


def mongo_goal_store() -> MongoGoalStore:
    return MongoGoalStore(db=FakeDB())


def atlas_goal_store() -> MongoGoalStore:
    return MongoGoalStore(db=atlas_db())


GOAL_STORES = [fakes.InMemoryGoalStore, mongo_goal_store, on_atlas(atlas_goal_store)]


def mongo_brief_builder() -> MongoBriefBuilder:
    return MongoBriefBuilder(db=FakeDB(), embedder=embedder())


def atlas_brief_builder() -> MongoBriefBuilder:
    return MongoBriefBuilder(db=atlas_db(), embedder=embedder())


BRIEF_BUILDERS = [fakes.FakeBriefBuilder, mongo_brief_builder, on_atlas(atlas_brief_builder)]


def openrouter_planner_on_fake_transport():
    reply = '{"intent": "Handle tabs per spec 2.2.", "strategy": "Expand tabs first."}'
    client = OpenRouterClient(
        transport=lambda payload, key: {"choices": [{"message": {"content": reply}}]},
        api_key="contract-test",
    )
    return OpenRouterPlanner(model="m", client=client)


PLANNERS = [fakes.FakePlanner, openrouter_planner_on_fake_transport]


def claude_runner_on_fake_binary():
    return make_runner(Path(tempfile.mkdtemp()))


def codex_runner_on_fake_binary():
    return make_codex_runner(Path(tempfile.mkdtemp()))


RUNNERS = [fakes.FakeRunner, claude_runner_on_fake_binary, codex_runner_on_fake_binary]


def spec_scorer_on_tiny_workspace() -> SpecScorer:
    """The real spec scorer over an echo renderer and a two-example split pair."""
    workspace = Path(tempfile.mkdtemp())
    hidden = Path(tempfile.mkdtemp())  # the harness-side splits stay outside the workspace (I1)
    (workspace / "render.py").write_text("import sys\nsys.stdout.write(sys.stdin.read())\n")
    passing = {"example": 1, "section": "Paragraphs", "markdown": "hi\n", "html": "hi\n"}
    failing = {"example": 2, "section": "ATX headings", "markdown": "# t\n", "html": "<h1>t</h1>\n"}
    val_only = {
        "example": 3,
        "section": "ATX headings",
        "markdown": "# x\n",
        "html": "<h1>x</h1>\n",
    }
    (hidden / "visible.json").write_text(json.dumps([passing, failing]))
    # Like the real split, every section present anywhere is present in validation.
    (hidden / "validation.json").write_text(json.dumps([passing, val_only]))
    return SpecScorer(
        workspace=workspace,
        visible=hidden / "visible.json",
        validation=hidden / "validation.json",
    )


def spec_scorer_on_fake_scorer():
    def fake(**kwargs):
        return {
            "visible_pass": 0.5,
            "val_pass": 0.25,
            "per_section": {
                "Tabs": {"visible": 1.0, "val": 0.5},
                "Precedence": {"visible": None, "val": 0.0},
            },
        }

    return SpecScorer(
        workspace=Path("ws"), visible=Path("v.json"), validation=Path("val.json"), score_fn=fake
    )


SCORERS = [fakes.ScriptedScorer, spec_scorer_on_fake_scorer, spec_scorer_on_tiny_workspace]
REVIEWERS = [fakes.FakeReviewer, GamingReviewer]


def mongo_ledger() -> MongoLedger:
    return MongoLedger(db=FakeDB(), embedder=embedder())


def atlas_ledger() -> MongoLedger:
    return MongoLedger(db=atlas_db(), embedder=embedder())


LEDGERS = [fakes.InMemoryLedger, mongo_ledger, on_atlas(atlas_ledger)]


class _StubSummarizer:
    def summarize(self, attempts) -> str:
        return "summary"


def mongo_compactor() -> MongoCompactor:
    return MongoCompactor(db=FakeDB(), embedder=embedder(), summarizer=_StubSummarizer())


def atlas_compactor() -> MongoCompactor:
    return MongoCompactor(db=atlas_db(), embedder=embedder(), summarizer=_StubSummarizer())


COMPACTORS = [fakes.FakeCompactor, mongo_compactor, on_atlas(atlas_compactor)]

GOAL = Goal(goal_id="c:Tabs", section="Tabs", target_val_pass=0.85)
RESULT = AttemptResult(agent="claude", commit="abc123", diff_summary="edit", exit_code=0)
SCORE = Score(visible_pass=0.6, val_pass=0.5, per_section={"Tabs": {"visible": 0.6, "val": 0.5}})


@pytest.mark.parametrize("make", GOAL_STORES)
def test_goal_store(make):
    store = make()
    assert isinstance(store, GoalStore)
    store.seed("c", ["Tabs", "Links"], 0.85)

    first = store.next_open("c")
    assert isinstance(first, Goal) and first.section in {"Tabs", "Links"}
    assert first.target_val_pass == 0.85

    store.replan(first.goal_id, "try a delimiter stack")
    assert store.next_open("c") is not None  # replanning keeps a goal open

    store.complete(first.goal_id)
    second = store.next_open("c")
    assert second is not None and second.goal_id != first.goal_id
    store.complete(second.goal_id)
    assert store.next_open("c") is None
    assert store.next_open("other-session") is None  # sessions are isolated


@pytest.mark.parametrize("make", GOAL_STORES)
def test_goal_store_replan_note_is_the_current_strategy(make):
    store = make()
    store.seed("r", ["Tabs"], 0.85)
    goal = store.next_open("r")
    assert goal.strategy_notes == ""
    store.replan(goal.goal_id, "expand tabs before block parsing")
    again = store.next_open("r")
    assert again.goal_id == goal.goal_id  # the only goal, so this always checks
    assert again.strategy_notes == "expand tabs before block parsing"


@pytest.mark.parametrize("make", BRIEF_BUILDERS)
@pytest.mark.parametrize("use_memory", [True, False])
def test_brief_builder(make, use_memory):
    builder = make()
    assert isinstance(builder, BriefBuilder)
    brief = builder.build("c", GOAL, use_memory=use_memory)
    assert isinstance(brief, str) and brief


@pytest.mark.parametrize("make", PLANNERS)
def test_planner(make):
    planner = make()
    assert isinstance(planner, Planner)
    intent = planner.plan(GOAL, "brief")
    assert isinstance(intent, str) and intent
    strategy = planner.replan(GOAL, "brief")
    assert isinstance(strategy, str) and strategy


@pytest.mark.parametrize("make", RUNNERS)
def test_attempt_runner(make):
    runner = make()
    assert isinstance(runner, AttemptRunner)
    assert isinstance(runner.agent, str) and runner.agent
    result = runner.run(session_id="c", attempt_id="c-001", brief="brief", intent="intent")
    assert isinstance(result, AttemptResult)
    assert result.agent == runner.agent and result.commit
    runner.reset_workspace(result.commit)
    runner.reset_workspace(None)


@pytest.mark.parametrize("make", SCORERS)
def test_scorer(make):
    scorer = make()
    assert isinstance(scorer, Scorer)
    score = scorer.score()
    assert isinstance(score, Score)
    assert 0 <= score.visible_pass <= 1 and 0 <= score.val_pass <= 1
    for rates in score.per_section.values():
        assert "val" in rates and set(rates) <= {"visible", "val"}  # visible: optional
        assert all(0 <= r <= 1 for r in rates.values())


@pytest.mark.parametrize("make", REVIEWERS)
@pytest.mark.parametrize("previous", [None, SCORE])
def test_reviewer(make, previous):
    reviewer = make()
    assert isinstance(reviewer, Reviewer)
    review = reviewer.review(RESULT, SCORE, previous)
    assert isinstance(review, Review) and isinstance(review.flagged, bool)
    if review.flagged:
        assert review.reasons  # a flag always says why (it goes to interventions)


@pytest.mark.parametrize("make", REVIEWERS)
def test_reviewer_accepts_sections_without_visible_examples(make):
    # Tiny spec sections have no visible examples, so real scores omit "visible" there (Score's
    # per_section contract). A reviewer must not reject such a score as invalid.
    tiny = Score(
        visible_pass=0.2,
        val_pass=0.2,
        per_section={"Tabs": {"visible": 0.2, "val": 0.2}, "Precedence": {"val": 0.0}},
    )
    later = Score(
        visible_pass=0.3,
        val_pass=0.3,
        per_section={"Tabs": {"visible": 0.3, "val": 0.3}, "Precedence": {"val": 1.0}},
    )
    for previous, score in ((None, tiny), (tiny, later)):
        review = make().review(RESULT, score, previous)
        assert not review.flagged, review.reasons


@pytest.mark.parametrize("make", LEDGERS)
def test_ledger(make):
    ledger = make()
    assert isinstance(ledger, Ledger)

    def open_(attempt_id, number):
        ledger.open_attempt(
            session_id="c",
            attempt_id=attempt_id,
            number=number,
            goal_id=GOAL.goal_id,
            agent="claude",
            intent="intent",
            parent_attempt=None,
        )

    def close(attempt_id, commit, outcome):
        result = AttemptResult(agent="claude", commit=commit, diff_summary="d", exit_code=0)
        flags = ["gamed"] if outcome == "flagged" else []
        ledger.close_attempt(
            attempt_id=attempt_id, result=result, score=SCORE, outcome=outcome, observer_flags=flags
        )

    assert ledger.last_clean_commit("c") is None
    open_("c-001", 1)
    close("c-001", "commit-1", "improved")
    open_("c-002", 2)
    close("c-002", "commit-2", "flagged")
    open_("c-003", 3)  # left running: the agent was killed

    assert ledger.last_clean_commit("c") == "commit-1"  # flagged attempts are never "clean"
    assert ledger.mark_running_as_killed("c") == ["c-003"]
    assert ledger.mark_running_as_killed("c") == []  # already recorded
    assert ledger.last_clean_commit("other-session") is None


@pytest.mark.parametrize("make", COMPACTORS)
def test_compactor(make):
    compactor = make()
    assert isinstance(compactor, Compactor)
    compactor.compact("c", GOAL.goal_id)
