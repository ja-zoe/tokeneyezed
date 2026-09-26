"""`tokeneyezed` operator CLI: start, resume, and inspect harness sessions.

    tokeneyezed run --config configs/h.toml [--session-id ID] [--fake] [--checkpointer memory]
    tokeneyezed resume ID --config configs/h.toml [--agent AGENT]
    tokeneyezed status ID
    tokeneyezed attempt --config configs/h.toml --intent "..." [--agent AGENT] [--brief-file F]
    tokeneyezed db init | check | backfill      (data/commands.py)
    tokeneyezed eval retrieval SESSION_ID       (data/commands.py)

Ctrl-C (or SIGTERM) kills a run; `resume` continues it from the latest checkpoint in Atlas.
Until the real ports exist, `run --fake` runs the loop on in-memory fakes. `resume` needs the real,
Atlas-backed ports: fakes live in one process, so there would be nothing to resume.
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime

from dotenv import load_dotenv
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver

from tokeneyezed.controller.config import RunConfig, load_config
from tokeneyezed.controller.fakes import fake_ports
from tokeneyezed.controller.graph import Context, build_graph, resume, start
from tokeneyezed.controller.live import LiveFeed
from tokeneyezed.ports import AttemptKilled, Ports

CHECKPOINT_DB = "tokeneyezed"


@contextmanager
def open_checkpointer(kind: str) -> Iterator[BaseCheckpointSaver]:
    if kind == "memory":
        yield InMemorySaver()
        return
    from langgraph.checkpoint.mongodb import MongoDBSaver

    uri = os.environ.get("MONGODB_URI")
    if not uri:
        sys.exit("MONGODB_URI is not set (copy .env.example to .env), or use --checkpointer memory")
    with MongoDBSaver.from_conn_string(
        uri,
        db_name=os.environ.get("TOKENEYEZED_DB") or CHECKPOINT_DB,
        checkpoint_collection_name="checkpoints",
        writes_collection_name="checkpoint_writes",
    ) as saver:
        yield saver


def make_ports(config: RunConfig, fake: bool) -> Ports:
    if not fake:
        sys.exit("The real ports aren't wired up yet; only `run --fake` works for now.")
    return fake_ports(agent=config.agent)


def _finished(feed: LiveFeed, state: dict, session_id: str) -> None:
    reason = state.get("finished", "stopped")
    feed.banner(f"{session_id} {reason.upper()} after {state['attempt_count']} attempts", "bold")


def cmd_run(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    session_id = args.session_id or f"{config.name}-{datetime.now(UTC):%m%d-%H%M%S}"
    ctx = Context(ports=make_ports(config, args.fake), config=config)
    feed = LiveFeed(agent=config.agent)
    with open_checkpointer(args.checkpointer) as saver:
        feed.banner(
            f"STARTED {session_id}  run {config.name}  agent {config.agent}  "
            f"{len(config.sections)} goals  budget {config.max_attempts} attempts"
        )
        try:
            _finished(feed, start(build_graph(saver), ctx, session_id, feed), session_id)
        except (KeyboardInterrupt, AttemptKilled):
            feed.line()
            feed.banner(f"KILLED during attempt #{feed.done + 1:03d}", "red")
            feed.line(f"   resume: tokeneyezed resume {session_id} --config {args.config}")
            return 130
    return 0


def cmd_resume(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    if args.agent:
        config = replace(config, agent=args.agent)
    ctx = Context(ports=make_ports(config, fake=False), config=config)
    feed = LiveFeed(agent=config.agent)

    with open_checkpointer("mongo") as saver:
        graph = build_graph(saver)
        if not graph.get_state({"configurable": {"thread_id": args.session_id}}).values:
            sys.exit(f"No checkpoint for session {args.session_id}")
        state = resume(
            graph,
            ctx,
            args.session_id,
            feed,
            lambda killed, restored, nxt: feed.resumed(
                args.session_id, config.agent, killed, restored, nxt
            ),
        )
        _finished(feed, state, args.session_id)
    return 0


def cmd_attempt(args: argparse.Namespace) -> int:
    """One real attempt, nothing else: smoke-tests the runner and runs the honeypot beat."""
    from tokeneyezed.controller.runners import runner_for

    config = load_config(args.config)
    runner = runner_for(args.agent or config.agent, config)
    brief = open(args.brief_file).read() if args.brief_file else ""
    session_id = args.session_id or f"attempt-{datetime.now(UTC):%m%d-%H%M%S}"
    attempt_id = f"{session_id}-001"
    feed = LiveFeed(agent=runner.agent)
    feed.banner(f"ATTEMPT {attempt_id}  agent {runner.agent}  workspace {runner.paths.workspace}")
    feed.section = "single attempt"
    feed.on_plan_attempt({"intent": args.intent})
    try:
        result = runner.run(
            session_id=session_id, attempt_id=attempt_id, brief=brief, intent=args.intent
        )
    except AttemptKilled:
        feed.banner("KILLED", "red")
        return 130
    feed.blocked(result.blocked)
    feed.line(f"      commit  {result.commit[:10]}  (agent exit code {result.exit_code})")
    for line in result.diff_summary.splitlines():
        feed.line(f"              {line}")
    feed.line(f"      transcript  {runner.paths.runs_dir / session_id / (attempt_id + '.jsonl')}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    with open_checkpointer("mongo") as saver:
        snapshot = build_graph(saver).get_state({"configurable": {"thread_id": args.session_id}})
    state = snapshot.values
    if not state:
        sys.exit(f"No checkpoint for session {args.session_id}")
    goal = (state.get("goal") or {}).get("section", "-")
    print(f"session   {args.session_id}")
    progress = state.get("finished") or f"in progress (next: {', '.join(snapshot.next)})"
    print(f"status    {progress}")
    print(f"attempts  {state.get('attempt_count', 0)}")
    print(f"goal      {goal}")
    for goal_id, best in sorted(state.get("best_val", {}).items()):
        print(f"  best val {best:.2f}  {goal_id}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tokeneyezed", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="start a new session")
    run.add_argument("--config", required=True)
    run.add_argument("--session-id")
    run.add_argument("--fake", action="store_true", help="use in-memory fake ports")
    run.add_argument("--checkpointer", choices=["mongo", "memory"], default="mongo")
    run.set_defaults(func=cmd_run)

    res = sub.add_parser("resume", help="continue a session from its latest checkpoint")
    res.add_argument("session_id")
    res.add_argument("--config", required=True)
    res.add_argument(
        "--agent", help="resume on a different agent than the one running (the agent handoff)"
    )
    res.set_defaults(func=cmd_resume)

    status = sub.add_parser("status", help="show a session's progress")
    status.add_argument("session_id")
    status.set_defaults(func=cmd_status)

    attempt = sub.add_parser("attempt", help="run one real attempt with the real runner")
    attempt.add_argument("--config", required=True)
    attempt.add_argument("--intent", required=True)
    attempt.add_argument("--agent", help="which agent runs it (default: the config's agent)")
    attempt.add_argument("--brief-file")
    attempt.add_argument("--session-id")
    attempt.set_defaults(func=cmd_attempt)

    from tokeneyezed.data import commands as data_commands
    from tokeneyezed.eval import commands as eval_commands

    data_commands.register(sub)  # db init|check|backfill, eval retrieval (docs/commands.md)
    eval_commands.register(sub)  # workspace init, heldout (docs/commands.md)

    args = parser.parse_args(argv)
    load_dotenv()
    # SIGTERM gets the same cleanup as Ctrl-C: the runner stops the agent's process group, which
    # would otherwise outlive the controller.
    signal.signal(signal.SIGTERM, signal.default_int_handler)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
