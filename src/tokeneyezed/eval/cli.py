"""Eval-lane subcommands for the `tokeneyezed` CLI: split, score, baseline, report.

Per docs/commands.md, each workstream keeps its commands in its own package and
exposes register(subparsers); controller/cli.py makes the one wiring call.

  tokeneyezed split [--seed N]                     build the visible/validation/heldout splits
  tokeneyezed score [--split visible|validation]   score the workspace, print the scorer JSON
  tokeneyezed baseline --config configs/b.toml     the naive retry loop, run B (STEP 3)
  tokeneyezed report [--sessions ...]              final numbers + score chart (STEP 5)

Machine-specific paths come from .env (see .env.example): TOKENEYEZED_WORKSPACE
is the task repo the agent works in; TOKENEYEZED_SPLITS_DIR holds the hidden
validation/heldout splits and must stay unreachable from the workspace
(invariant I1 — `split` refuses to nest one inside the other). Shared run
values (max_attempts, max_turns, target_val_pass) are read from the run config
extending configs/base.toml, never hardcoded here.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from tokeneyezed.controller.config import load_config
from tokeneyezed.eval import scorer, split

DEFAULT_SPLITS_DIR = "~/.tokeneyezed/splits"


def _env_path(name: str, default: str | None = None) -> Path:
    value = os.environ.get(name) or default
    if not value:
        sys.exit(f"{name} is not set (copy .env.example to .env and fill it in)")
    return Path(value).expanduser()


def workspace_dir() -> Path:
    """The task repo the agent works in (TOKENEYEZED_WORKSPACE, from .env)."""
    return _env_path("TOKENEYEZED_WORKSPACE")


def splits_dir() -> Path:
    """The harness-side dir for the scorer's visible copy, validation, heldout, and
    manifest, outside the workspace (I1)."""
    return _env_path("TOKENEYEZED_SPLITS_DIR", DEFAULT_SPLITS_DIR)


def _require(path: Path, hint: str) -> str:
    if not path.exists():
        sys.exit(f"{path} not found — {hint}")
    return str(path)


def _leaking_symlink(ws: Path, hidden: Path) -> Path | None:
    """A symlink under ws whose resolution reaches the hidden splits dir, or None."""
    for root, dirs, files in os.walk(ws):
        for name in dirs + files:
            path = Path(root) / name
            if not path.is_symlink():
                continue
            target = path.resolve()
            if hidden == target or hidden.is_relative_to(target) or target.is_relative_to(hidden):
                return path
    return None


def cmd_split(args: argparse.Namespace) -> int:
    """Build the three splits: visible into the workspace, the hidden pair outside it.

    split.write_splits() guards the visible file's directory; the CLI also knows
    the real workspace root, so it refuses a splits dir nested under it and any
    workspace symlink that resolves to it (I1, checked at split time — the
    observer's protected paths guard the workspace during runs).
    """
    ws = workspace_dir().resolve()
    hidden = splits_dir().resolve()
    if hidden.is_relative_to(ws) or ws.is_relative_to(hidden):
        sys.exit(
            f"refusing: TOKENEYEZED_SPLITS_DIR {hidden} is reachable from the task "
            f"workspace {ws} (invariant I1)"
        )
    if link := _leaking_symlink(ws, hidden):
        sys.exit(
            f"refusing: workspace symlink {link} resolves into the hidden splits "
            f"dir {hidden} (invariant I1)"
        )
    return split.main(
        [
            "--hidden-dir",
            str(hidden),
            "--visible-dest",
            str(ws / "tests" / "visible.json"),
            "--workspace",
            str(ws),
            "--seed",
            str(args.seed),
        ]
    )


def cmd_score(args: argparse.Namespace) -> int:
    """Score the workspace renderer and print the scorer JSON (backs Julian's Scorer port).

    Both split files come from the harness-side splits dir — visible from the
    copy `split` wrote there, never the agent-writable tests/visible.json, so
    an edited workspace copy cannot inflate visible_pass.
    """
    ws = workspace_dir()
    hint = "run `tokeneyezed split` first"
    files = {
        "visible": splits_dir() / "visible.json",
        "validation": splits_dir() / "validation.json",
    }
    common = ["--workspace", str(ws), "--program", args.program, "--jobs", str(args.jobs)]
    if args.split:
        return scorer.main(["single", "--file", _require(files[args.split], hint), *common])
    return scorer.main(
        [
            "attempt",
            "--visible",
            _require(files["visible"], hint),
            "--validation",
            _require(files["validation"], hint),
            *common,
        ]
    )


def cmd_baseline(args: argparse.Namespace) -> int:
    """Run B, the naive retry loop. Loads the run config now; the loop itself is STEP 3."""
    config = load_config(args.config)
    sys.exit(
        f"the baseline loop isn't built yet (STEP 3); run {config.name} would use agent "
        f"{config.agent}, {config.max_attempts} attempts x {config.max_turns} turns "
        f"(from {args.config} extending base.toml)"
    )


def cmd_report(args: argparse.Namespace) -> int:
    """Final numbers and the score chart, the only reader of `test_evals` (STEP 5)."""
    sessions = " ".join(args.sessions) if args.sessions else "B/H/H-mem"
    sys.exit(
        f"`tokeneyezed report` isn't built yet (STEP 5): it will read `attempts` and "
        f"`test_evals` for sessions {sessions} and chart held-out pass rate vs attempt number"
    )


def register(subparsers: argparse._SubParsersAction) -> None:
    """Add the eval-lane subcommands (docs/commands.md, "Task and eval") to the CLI."""
    sp = subparsers.add_parser("split", help="build the visible/validation/heldout spec splits")
    sp.add_argument("--seed", type=int, default=split.DEFAULT_SEED)
    sp.set_defaults(func=cmd_split)

    sc = subparsers.add_parser("score", help="score the task workspace; print the scorer JSON")
    sc.add_argument(
        "--split",
        choices=["visible", "validation"],
        help="score one split alone (default: both, in the scorer's attempt mode)",
    )
    sc.add_argument(
        "--program",
        default=scorer.DEFAULT_PROGRAM,
        help="renderer command: markdown on stdin -> HTML on stdout",
    )
    sc.add_argument("--jobs", type=int, default=8)
    sc.set_defaults(func=cmd_score)

    base = subparsers.add_parser("baseline", help="run B: the naive retry loop, no observer")
    base.add_argument("--config", required=True, help="run config, e.g. configs/b.toml")
    base.set_defaults(func=cmd_baseline)

    rep = subparsers.add_parser("report", help="final numbers + score chart at equal attempts")
    rep.add_argument("--sessions", nargs="*", default=[], help="session ids, e.g. B-.. H-..")
    rep.set_defaults(func=cmd_report)
