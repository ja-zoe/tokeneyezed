"""CLI commands for the eval package (docs/commands.md); controller/cli.py calls register()."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any


def cmd_workspace_init(args: argparse.Namespace) -> int:
    from tokeneyezed.eval.workspace import init_workspace

    ws = init_workspace(Path(args.dir), Path(args.visible))
    print(f"task workspace ready: {ws}")
    print(f"set TOKENEYEZED_WORKSPACE={ws} in .env")
    return 0


def cmd_heldout(args: argparse.Namespace) -> int:
    from tokeneyezed.eval.heldout import score_session_in_atlas

    written = score_session_in_atlas(
        args.session_id, Path(args.workspace), Path(args.heldout), args.program
    )
    for doc in written:
        print(f"#{doc['number']:03d}  {doc['agent'] or '-':<7} held-out {doc['test_pass']:.3f}")
    print(f"{len(written)} attempt(s) scored")
    return 0


def register(subparsers: Any) -> None:
    ws = subparsers.add_parser("workspace", help="the agent's task workspace").add_subparsers(
        dest="workspace_command", required=True
    )
    init = ws.add_parser("init", help="create the task repo the agent works in")
    init.add_argument("dir", help="an empty directory outside this repo")
    init.add_argument("--visible", required=True, help="the visible split file (eval/split.py)")
    init.set_defaults(func=cmd_workspace_init)

    held = subparsers.add_parser("heldout", help="score a session's attempts on the held-out split")
    held.add_argument("session_id")
    held.add_argument("--workspace", required=True, help="the session's task workspace")
    held.add_argument("--heldout", required=True, help="heldout.json from the hidden split dir")
    held.add_argument("--program", default="python3 render.py")
    held.set_defaults(func=cmd_heldout)
