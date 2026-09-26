"""Contract tests: every implementation of a port, fake or real, must pass these.

To plug in a real implementation, add a factory to that port's list below. If it needs Atlas or an
API key, wrap it in `pytest.param(..., marks=needs_env("MONGODB_URI"))` so it skips cleanly
without credentials (and CI still runs the fakes).
"""

import os

import pytest
from mongo_fakes import FakeDB, embedder

from tokeneyezed.controller import fakes
from tokeneyezed.data.brief import MongoBriefBuilder
from tokeneyezed.data.ledger import MongoLedger
from tokeneyezed.observer.reviewer import GamingReviewer
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


GOAL_STORES = [fakes.InMemoryGoalStore]


def mongo_brief_builder() -> MongoBriefBuilder:
    return MongoBriefBuilder(db=FakeDB(), embedder=embedder())


BRIEF_BUILDERS = [fakes.FakeBriefBuilder, mongo_brief_builder]
PLANNERS = [fakes.FakePlanner]
RUNNERS = [fakes.FakeRunner]
SCORERS = [fakes.ScriptedScorer]
REVIEWERS = [fakes.FakeReviewer, GamingReviewer]


def mongo_ledger() -> MongoLedger:
    return MongoLedger(db=FakeDB(), embedder=embedder())


LEDGERS = [fakes.InMemoryLedger, mongo_ledger]
COMPACTORS = [fakes.FakeCompactor]

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
        assert set(rates) >= {"visible", "val"}
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
