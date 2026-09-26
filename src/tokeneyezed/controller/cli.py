"""`tokeneyezed` operator CLI: start, resume, and inspect harness sessions.

    tokeneyezed run --config configs/h.toml [--session-id ID] [--fake] [--checkpointer memory]
    tokeneyezed resume ID --config configs/h.toml [--agent codex]
    tokeneyezed status ID

Ctrl-C (or SIGTERM) kills a run; `resume` continues it from the latest checkpoint in Atlas.
Until the real ports exist, `run --fake` runs the loop on in-memory fakes. `resume` needs the real,
Atlas-backed ports: fakes live in one process, so there would be nothing to resume.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver

from tokeneyezed.controller.config import RunConfig, load_config
from tokeneyezed.controller.fakes import fake_ports
from tokeneyezed.controller.graph import Context, build_graph, resume, start
from tokeneyezed.controller.ports import AttemptKilled, Ports

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


def _report(state: dict, session_id: str) -> None:
    print(
        f"{session_id}: {state.get('finished', 'stopped')} after {state['attempt_count']} attempts"
    )


def cmd_run(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    session_id = args.session_id or f"{config.name}-{datetime.now(UTC):%m%d-%H%M%S}"
    ctx = Context(ports=make_ports(config, args.fake), config=config)
    with open_checkpointer(args.checkpointer) as saver:
        print(f"Starting {session_id} ({config.name}, agent {config.agent})")
        try:
            _report(start(build_graph(saver), ctx, session_id), session_id)
        except (KeyboardInterrupt, AttemptKilled):
            print(
                f"\nKilled. Continue with: tokeneyezed resume {session_id} --config {args.config}"
            )
            return 130
    return 0


def cmd_resume(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    if args.agent:
        config = replace(config, agent=args.agent)
    ctx = Context(ports=make_ports(config, fake=False), config=config)
    with open_checkpointer("mongo") as saver:
        graph = build_graph(saver)
        if not graph.get_state({"configurable": {"thread_id": args.session_id}}).values:
            sys.exit(f"No checkpoint for session {args.session_id}")
        print(f"Resuming {args.session_id} with agent {config.agent}")
        _report(resume(graph, ctx, args.session_id), args.session_id)
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
    res.add_argument("--agent", help="resume with a different agent, e.g. codex")
    res.set_defaults(func=cmd_resume)

    status = sub.add_parser("status", help="show a session's progress")
    status.add_argument("session_id")
    status.set_defaults(func=cmd_status)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
