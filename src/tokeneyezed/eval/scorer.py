"""Score a Markdown-to-HTML renderer against CommonMark spec-example splits.

Renderer contract (agent-agnostic): the renderer is any command that reads
Markdown (UTF-8) on stdin and writes HTML on stdout, run with the task
workspace as cwd. Default: `python3 render.py`. Non-zero exit, a timeout, or
output that differs from the spec's expected HTML all count as a fail.
Comparison is exact string match, tolerating only a trailing-newline
difference. Invalid UTF-8 in the renderer's output is decoded with
errors="replace", so it becomes an ordinary fail instead of crashing the run.

The renderer runs with a minimal environment (PATH, LANG/LC_ALL, a throwaway
HOME) — never the harness's, whose MONGODB_URI and API keys agent-written code
must not see. Known residual hole (same Unix user, no sandbox): a renderer can
still read any file by absolute path, including the hidden splits; mitigate by
keeping TOKENEYEZED_SPLITS_DIR out of anything the agent sees and on the
observer's protected-paths list.

Modes and output JSON (the contract locked at 10:45 — do not rename fields):

  attempt   scores visible + validation; Julian's attempt runner merges this
            straight into the `attempts` document:
    {"scorer_version": "0.1-draft", "spec_version": "0.31.2",
     "visible_pass": 0.8061, "val_pass": 0.5789,
     "per_section": {"Tabs": {"visible": 1.0, "val": 0.75}, ...},
     "counts": {"visible":    {"total": 196, "passed": 158, "failed": 34,
                               "errors": 3, "timeouts": 1},
                "validation": {...}},
     "duration_s": 14.2}
            per_section values are null for a split with no examples in that
            section (tiny sections don't cover all splits).

  heldout   scores the held-out split; goes ONLY to `test_evals` (I3):
    {"scorer_version": ..., "spec_version": ..., "test_pass": 0.55,
     "per_section": {"Tabs": 0.5, ...}, "counts": {...}, "duration_s": ...}

  single    scores any one split file (debugging):
    {"scorer_version": ..., "spec_version": ..., "pass_rate": ...,
     "per_section": {...}, "counts": {...}, "duration_s": ...}

Usage:
  python3 -m tokeneyezed.eval.scorer attempt --visible v.json --validation val.json \
      --workspace /path/to/task-repo [--program "python3 render.py"] [--jobs 8]
  python3 -m tokeneyezed.eval.scorer heldout --heldout h.json --workspace ...
  python3 -m tokeneyezed.eval.scorer single --file any.json --workspace ...
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
from collections import defaultdict
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from functools import partial

SCORER_VERSION = "0.1-draft"
SPEC_VERSION = "0.31.2"
DEFAULT_PROGRAM = "python3 render.py"
DEFAULT_TIMEOUT = 5.0


def renderer_env(home: str) -> dict[str, str]:
    """A minimal environment for the renderer subprocess.

    Only PATH (so the renderer command resolves), a fixed UTF-8 locale, and a
    throwaway HOME. The harness's own environment — MONGODB_URI, OPENROUTER and
    Voyage keys — must never reach agent-written code.
    """
    return {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "HOME": home,
    }


def run_example(
    example: dict, program: list[str], workspace: str, timeout: float, env: dict[str, str]
) -> str:
    """Render one spec example; return 'passed', 'failed', 'error', or 'timeout'."""
    try:
        proc = subprocess.run(
            program,
            input=example["markdown"],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            cwd=workspace,
            timeout=timeout,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return "timeout"
    except OSError:
        return "error"
    if proc.returncode != 0:
        return "error"
    got, want = proc.stdout, example["html"]
    return "passed" if got == want or got.rstrip("\n") == want.rstrip("\n") else "failed"


def score_split(path: str, program: list[str], workspace: str, timeout: float, jobs: int) -> dict:
    """Score every example in one split file.

    Returns {"counts": {...}, "pass_rate": float, "section_pass": {section: rate}}.
    """
    with open(path) as f:
        examples = json.load(f)
    with tempfile.TemporaryDirectory(prefix="tokeneyezed-renderer-home-") as home:
        render = partial(
            run_example,
            program=program,
            workspace=workspace,
            timeout=timeout,
            env=renderer_env(home),
        )
        with ThreadPoolExecutor(max_workers=jobs) as pool:
            outcomes = list(pool.map(render, examples))
    counts = {"total": len(examples), "passed": 0, "failed": 0, "errors": 0, "timeouts": 0}
    by_section: dict[str, list[bool]] = defaultdict(list)
    key = {"passed": "passed", "failed": "failed", "error": "errors", "timeout": "timeouts"}
    for ex, outcome in zip(examples, outcomes, strict=True):
        counts[key[outcome]] += 1
        by_section[ex["section"]].append(outcome == "passed")
    rate = round(counts["passed"] / counts["total"], 4) if counts["total"] else 0.0
    section_pass = {s: round(sum(v) / len(v), 4) for s, v in sorted(by_section.items())}
    return {"counts": counts, "pass_rate": rate, "section_pass": section_pass}


def score_attempt(
    visible: str,
    validation: str,
    workspace: str,
    program: str = DEFAULT_PROGRAM,
    timeout: float = DEFAULT_TIMEOUT,
    jobs: int = 8,
) -> dict:
    """Score visible + validation splits; the result merges into the `attempts` doc."""
    cmd = shlex.split(program)
    start = time.monotonic()
    vis = score_split(visible, cmd, workspace, timeout, jobs)
    val = score_split(validation, cmd, workspace, timeout, jobs)
    sections = sorted(set(vis["section_pass"]) | set(val["section_pass"]))
    return {
        "scorer_version": SCORER_VERSION,
        "spec_version": SPEC_VERSION,
        "visible_pass": vis["pass_rate"],
        "val_pass": val["pass_rate"],
        "per_section": {
            s: {"visible": vis["section_pass"].get(s), "val": val["section_pass"].get(s)}
            for s in sections
        },
        "counts": {"visible": vis["counts"], "validation": val["counts"]},
        "duration_s": round(time.monotonic() - start, 2),
    }


def score_heldout(
    heldout: str,
    workspace: str,
    program: str = DEFAULT_PROGRAM,
    timeout: float = DEFAULT_TIMEOUT,
    jobs: int = 8,
) -> dict:
    """Score the held-out split; the result goes ONLY to `test_evals` (invariant I3)."""
    start = time.monotonic()
    res = score_split(heldout, shlex.split(program), workspace, timeout, jobs)
    return {
        "scorer_version": SCORER_VERSION,
        "spec_version": SPEC_VERSION,
        "test_pass": res["pass_rate"],
        "per_section": res["section_pass"],
        "counts": res["counts"],
        "duration_s": round(time.monotonic() - start, 2),
    }


def score_single(
    file: str,
    workspace: str,
    program: str = DEFAULT_PROGRAM,
    timeout: float = DEFAULT_TIMEOUT,
    jobs: int = 8,
) -> dict:
    """Score any one split file (debugging)."""
    start = time.monotonic()
    res = score_split(file, shlex.split(program), workspace, timeout, jobs)
    return {
        "scorer_version": SCORER_VERSION,
        "spec_version": SPEC_VERSION,
        "pass_rate": res["pass_rate"],
        "per_section": res["section_pass"],
        "counts": res["counts"],
        "duration_s": round(time.monotonic() - start, 2),
    }


def main(argv: Sequence[str] | None = None) -> int:
    """Parse the mode and split files, score them, print the result JSON to stdout."""
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("mode", choices=["attempt", "heldout", "single"])
    ap.add_argument("--visible", help="visible split file (attempt mode)")
    ap.add_argument("--validation", help="validation split file (attempt mode)")
    ap.add_argument("--heldout", help="held-out split file (heldout mode)")
    ap.add_argument("--file", help="any split file (single mode)")
    ap.add_argument("--workspace", required=True, help="task repo; the renderer's cwd")
    ap.add_argument(
        "--program",
        default=DEFAULT_PROGRAM,
        help="renderer command: markdown on stdin -> HTML on stdout",
    )
    ap.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="seconds per example")
    ap.add_argument("--jobs", type=int, default=8)
    args = ap.parse_args(argv)

    if args.mode == "attempt":
        if not (args.visible and args.validation):
            ap.error("attempt mode needs --visible and --validation")
        out = score_attempt(
            args.visible, args.validation, args.workspace, args.program, args.timeout, args.jobs
        )
    elif args.mode == "heldout":
        if not args.heldout:
            ap.error("heldout mode needs --heldout")
        out = score_heldout(args.heldout, args.workspace, args.program, args.timeout, args.jobs)
    else:
        if not args.file:
            ap.error("single mode needs --file")
        out = score_single(args.file, args.workspace, args.program, args.timeout, args.jobs)

    json.dump(out, sys.stdout, indent=1)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
