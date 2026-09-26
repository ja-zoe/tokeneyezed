"""The controller: the master plan's goal loop as a LangGraph StateGraph.

State is small and checkpointed (ids, counters, the current attempt). Goals, attempts, and memory
live in Atlas behind the ports. The ports and run config arrive through the runtime context,
which is not checkpointed: resuming a session with a different AttemptRunner is the Codex handoff.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any, Literal, TypedDict

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.runtime import Runtime

from tokeneyezed.controller.config import RunConfig
from tokeneyezed.ports import AttemptResult, Goal, Ports, Score

NODES_PER_ATTEMPT = 9  # graph steps in one pass through the loop, for the recursion limit


@dataclass
class Context:
    ports: Ports
    config: RunConfig


class State(TypedDict, total=False):
    session_id: str
    attempt_count: int  # finished attempts, flagged included; killed attempts don't count
    goal: dict[str, Any] | None  # the active Goal
    brief: str
    intent: str
    attempt_id: str
    result: dict[str, Any]  # AttemptResult
    score: dict[str, Any]  # Score
    flags: list[str]
    best_val: dict[str, float]  # goal_id -> best clean validation pass rate
    last_clean: dict[str, dict[str, Any]]  # goal_id -> {"attempt_id", "score"}
    streaks: dict[str, int]  # goal_id -> consecutive clean attempts without improvement
    finished: str  # why the run ended


def _goal(state: State) -> Goal:
    return Goal(**state["goal"])


def seed_goals(state: State, runtime: Runtime[Context]) -> dict:
    cfg = runtime.context.config
    runtime.context.ports.goals.seed(state["session_id"], cfg.sections, cfg.target_val_pass)
    return {"attempt_count": 0, "best_val": {}, "last_clean": {}, "streaks": {}}


def pick_goal(state: State, runtime: Runtime[Context]) -> dict:
    if state["attempt_count"] >= runtime.context.config.max_attempts:
        return {"goal": None, "finished": "budget spent"}
    goal = runtime.context.ports.goals.next_open(state["session_id"])
    if goal is None:
        return {"goal": None, "finished": "all goals complete"}
    return {"goal": asdict(goal)}


def route_after_pick(state: State) -> Literal["build_brief", "finalize"]:
    return "finalize" if state["goal"] is None else "build_brief"


def build_brief(state: State, runtime: Runtime[Context]) -> dict:
    brief = runtime.context.ports.brief.build(
        state["session_id"], _goal(state), use_memory=runtime.context.config.memory
    )
    return {"brief": brief}


def plan_attempt(state: State, runtime: Runtime[Context]) -> dict:
    return {"intent": runtime.context.ports.planner.plan(_goal(state), state["brief"])}


def run_attempt(state: State, runtime: Runtime[Context]) -> dict:
    # The attempt id is allocated here, not in plan_attempt: if the agent is killed, this node
    # never commits, and on resume it re-runs with a fresh id while the killed one stays "killed".
    ports, goal = runtime.context.ports, _goal(state)
    number = state["attempt_count"] + 1
    attempt_id = f"{state['session_id']}-{number:03d}-{uuid.uuid4().hex[:6]}"
    parent = state["last_clean"].get(goal.goal_id, {}).get("attempt_id")
    ports.ledger.open_attempt(
        session_id=state["session_id"],
        attempt_id=attempt_id,
        number=number,
        goal_id=goal.goal_id,
        agent=ports.runner.agent,
        intent=state["intent"],
        parent_attempt=parent,
    )
    result = ports.runner.run(
        session_id=state["session_id"],
        attempt_id=attempt_id,
        brief=state["brief"],
        intent=state["intent"],
    )
    return {"attempt_id": attempt_id, "result": asdict(result)}


def score_attempt(state: State, runtime: Runtime[Context]) -> dict:
    score = runtime.context.ports.scorer.score()
    return {"score": {**asdict(score), "per_section": dict(score.per_section)}}


def review_attempt(state: State, runtime: Runtime[Context]) -> dict:
    goal = _goal(state)
    previous = state["last_clean"].get(goal.goal_id, {}).get("score")
    review = runtime.context.ports.reviewer.review(
        AttemptResult(**state["result"]),
        Score(**state["score"]),
        Score(**previous) if previous else None,
    )
    return {"flags": list(review.reasons) if review.flagged else []}


def route_after_review(state: State) -> Literal["record_flagged", "record_attempt"]:
    return "record_flagged" if state["flags"] else "record_attempt"


def record_flagged(state: State, runtime: Runtime[Context]) -> dict:
    # Audit only: never touches best scores, streaks, the compactor, or the parent chain (I8).
    runtime.context.ports.ledger.close_attempt(
        attempt_id=state["attempt_id"],
        result=AttemptResult(**state["result"]),
        score=Score(**state["score"]),
        outcome="flagged",
        observer_flags=state["flags"],
    )
    return {"attempt_count": state["attempt_count"] + 1}


def record_attempt(state: State, runtime: Runtime[Context]) -> dict:
    goal, score = _goal(state), Score(**state["score"])
    val = score.section_val(goal.section)
    best = state["best_val"].get(goal.goal_id)
    improved = best is None or val > best
    runtime.context.ports.ledger.close_attempt(
        attempt_id=state["attempt_id"],
        result=AttemptResult(**state["result"]),
        score=score,
        outcome="improved" if improved else "no_improvement",
        observer_flags=[],
    )
    streak = 0 if improved else state["streaks"].get(goal.goal_id, 0) + 1
    return {
        "attempt_count": state["attempt_count"] + 1,
        "best_val": {**state["best_val"], goal.goal_id: val if improved else best},
        "streaks": {**state["streaks"], goal.goal_id: streak},
        "last_clean": {
            **state["last_clean"],
            goal.goal_id: {"attempt_id": state["attempt_id"], "score": state["score"]},
        },
    }


def route_after_record(
    state: State, runtime: Runtime[Context]
) -> Literal["complete_goal", "replan", "compact"]:
    goal = _goal(state)
    if state["best_val"][goal.goal_id] >= goal.target_val_pass:
        return "complete_goal"
    if state["streaks"][goal.goal_id] >= runtime.context.config.failure_threshold:
        return "replan"
    return "compact"


def complete_goal(state: State, runtime: Runtime[Context]) -> dict:
    runtime.context.ports.goals.complete(state["goal"]["goal_id"])
    return {}


def replan(state: State, runtime: Runtime[Context]) -> dict:
    goal = _goal(state)
    streak, best = state["streaks"][goal.goal_id], state["best_val"][goal.goal_id]
    runtime.context.ports.goals.replan(
        goal.goal_id, f"{streak} attempts without improvement; best validation {best:.2f}"
    )
    return {"streaks": {**state["streaks"], goal.goal_id: 0}}


def compact(state: State, runtime: Runtime[Context]) -> dict:
    runtime.context.ports.compactor.compact(state["session_id"], state["goal"]["goal_id"])
    return {}


def finalize(state: State) -> dict:
    return {}


def build_graph(checkpointer: BaseCheckpointSaver | None = None) -> CompiledStateGraph:
    builder = StateGraph(State, context_schema=Context)
    for name, node in [
        ("seed_goals", seed_goals),
        ("pick_goal", pick_goal),
        ("build_brief", build_brief),
        ("plan_attempt", plan_attempt),
        ("run_attempt", run_attempt),
        ("score_attempt", score_attempt),
        ("review_attempt", review_attempt),
        ("record_flagged", record_flagged),
        ("record_attempt", record_attempt),
        ("complete_goal", complete_goal),
        ("replan", replan),
        ("compact", compact),
        ("finalize", finalize),
    ]:
        builder.add_node(name, node)
    builder.add_edge(START, "seed_goals")
    builder.add_edge("seed_goals", "pick_goal")
    builder.add_conditional_edges("pick_goal", route_after_pick)
    builder.add_edge("build_brief", "plan_attempt")
    builder.add_edge("plan_attempt", "run_attempt")
    builder.add_edge("run_attempt", "score_attempt")
    builder.add_edge("score_attempt", "review_attempt")
    builder.add_conditional_edges("review_attempt", route_after_review)
    builder.add_edge("record_flagged", "pick_goal")
    builder.add_conditional_edges("record_attempt", route_after_record)
    for node in ("complete_goal", "replan", "compact"):
        builder.add_edge(node, "pick_goal")
    builder.add_edge("finalize", END)
    return builder.compile(checkpointer=checkpointer)


def _run_config(session_id: str, config: RunConfig) -> dict:
    limit = (config.max_attempts + 2) * NODES_PER_ATTEMPT + 10
    return {"configurable": {"thread_id": session_id}, "recursion_limit": limit}


OnUpdate = Callable[[str, dict[str, Any]], None]


def _drive(
    graph: CompiledStateGraph,
    graph_input: State | None,
    ctx: Context,
    session_id: str,
    on_update: OnUpdate | None,
) -> State:
    run_config = _run_config(session_id, ctx.config)
    for chunk in graph.stream(graph_input, run_config, context=ctx, stream_mode="updates"):
        for node, update in chunk.items():
            if on_update:
                on_update(node, update or {})
    return graph.get_state(run_config).values


def start(
    graph: CompiledStateGraph, ctx: Context, session_id: str, on_update: OnUpdate | None = None
) -> State:
    """Start a new session and run it until it finishes or the agent is killed."""
    return _drive(graph, {"session_id": session_id}, ctx, session_id, on_update)


def resume(
    graph: CompiledStateGraph,
    ctx: Context,
    session_id: str,
    on_update: OnUpdate | None = None,
    on_resumed: Callable[[list[str], State, tuple[str, ...]], None] | None = None,
) -> State:
    """Continue a session from its latest checkpoint, possibly with a different runner.

    Any attempt still marked running was killed: record it so, and reset the workspace to the last
    clean attempt's commit so its half-finished edits are gone. on_resumed receives the killed
    attempt ids, the restored state, and the next node(s) before the loop continues.
    """
    killed = ctx.ports.ledger.mark_running_as_killed(session_id)
    ctx.ports.runner.reset_workspace(ctx.ports.ledger.last_clean_commit(session_id))
    if on_resumed:
        snapshot = graph.get_state(_run_config(session_id, ctx.config))
        on_resumed(killed, snapshot.values, tuple(snapshot.next))
    return _drive(graph, None, ctx, session_id, on_update)
