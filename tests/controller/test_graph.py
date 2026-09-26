"""The controller spec's merge gate (docs/specs/controller-graph.md), run on fakes."""

from dataclasses import replace

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from tokeneyezed.controller.config import load_config
from tokeneyezed.controller.fakes import (
    FakeReviewer,
    FakeRunner,
    ScriptedScorer,
    fake_ports,
)
from tokeneyezed.controller.graph import Context, build_graph, resume, start
from tokeneyezed.controller.ports import AttemptKilled

BASE = replace(
    load_config("configs/h.toml"),
    sections=("Tabs", "Links"),
    max_attempts=20,
    failure_threshold=3,
    target_val_pass=0.85,
)


def run(ports, config=BASE, session_id="s1"):
    graph = build_graph(InMemorySaver())
    return graph, start(graph, Context(ports=ports, config=config), session_id)


def test_happy_path_completes_every_goal():
    ports = fake_ports(scorer=ScriptedScorer(val_script=(0.5, 0.9)))
    _, state = run(ports)

    assert state["finished"] == "all goals complete"
    assert all(g["status"] == "complete" for g in ports.goals.goals.values())
    closed = ports.ledger.closed("s1")
    assert [a["number"] for a in closed] == [1, 2, 3]  # Tabs: 0.5, 0.9 (done); Links: 0.9
    assert [a["outcome"] for a in closed] == ["improved"] * 3


def test_budget_counts_flagged_attempts():
    config = replace(BASE, max_attempts=4)
    ports = fake_ports(
        scorer=ScriptedScorer(val_script=(0.1,)),  # never completes a goal
        reviewer=FakeReviewer(flag_calls=frozenset({1, 2, 3})),
    )
    _, state = run(ports, config)

    assert state["finished"] == "budget spent"
    assert state["attempt_count"] == 4
    assert [a["outcome"] for a in ports.ledger.closed("s1")] == ["flagged"] * 3 + ["improved"]


def test_flagged_attempts_stay_out_of_memory_and_streaks():
    config = replace(BASE, max_attempts=3)
    ports = fake_ports(
        scorer=ScriptedScorer(val_script=(0.2, 0.9, 0.3)),
        reviewer=FakeReviewer(flag_calls=frozenset({2})),  # the 0.9 attempt looked gamed
    )
    _, state = run(ports, config)

    goal_id = "s1:Tabs"
    assert state["best_val"][goal_id] == 0.3  # the flagged 0.9 never counted
    assert ports.goals.goals[goal_id]["status"] == "open"
    flagged = [a for a in ports.ledger.closed("s1") if a["outcome"] == "flagged"]
    assert len(flagged) == 1 and flagged[0]["observer_flags"]
    assert len(ports.compactor.calls) == 2  # only the two clean attempts reached the compactor
    clean = [a for a in ports.ledger.closed("s1") if a["outcome"] != "flagged"]
    assert clean[1]["parent_attempt"] == clean[0]["attempt_id"]  # the flagged one is skipped


def test_replan_after_failure_streak():
    config = replace(BASE, max_attempts=5, failure_threshold=3)
    ports = fake_ports(scorer=ScriptedScorer(val_script=(0.4,)))  # 1 improvement, then flat
    run(ports, config)

    # Attempt 1 improves (streak 0); attempts 2-4 are flat -> replan after attempt 4 resets the
    # streak; attempt 5 is flat again (streak 1): exactly one replan.
    assert len(ports.goals.replans) == 1
    goal_id, note = ports.goals.replans[0]
    assert goal_id == "s1:Tabs" and "3 attempts without improvement" in note


def test_kill_then_resume_on_another_agent_continues_without_repeating():
    ledger_scorer = ScriptedScorer(val_script=(0.3, 0.5, 0.6, 0.9, 0.9))
    claude = fake_ports(
        agent="claude",
        scorer=ledger_scorer,
        runner=FakeRunner(agent="claude", kill_on_call=3),
    )
    graph = build_graph(InMemorySaver())
    with pytest.raises(AttemptKilled):
        start(graph, Context(ports=claude, config=BASE), "s1")

    # Same Atlas state (goals, ledger, scorer's workspace), different agent.
    codex_runner = FakeRunner(agent="codex")
    codex = fake_ports(
        agent="codex",
        goals=claude.goals,
        ledger=claude.ledger,
        scorer=ledger_scorer,
        compactor=claude.compactor,
        runner=codex_runner,
    )
    state = resume(graph, Context(ports=codex, config=BASE), "s1")

    assert state["finished"] == "all goals complete"
    attempts = claude.ledger.attempts.values()
    killed = [a for a in attempts if a["status"] == "killed"]
    assert len(killed) == 1 and killed[0]["agent"] == "claude" and killed[0]["number"] == 3
    closed = claude.ledger.closed("s1")
    assert [a["number"] for a in closed] == list(range(1, len(closed) + 1))  # no repeats or gaps
    assert [a["agent"] for a in closed] == ["claude", "claude"] + ["codex"] * (len(closed) - 2)
    assert codex_runner.resets == [closed[1]["commit"]]  # reset to the last clean commit


def test_memory_flag_reaches_the_brief_builder():
    ports = fake_ports()
    run(ports, replace(BASE, memory=False))
    assert ports.brief.calls and all(use_memory is False for _, use_memory in ports.brief.calls)
