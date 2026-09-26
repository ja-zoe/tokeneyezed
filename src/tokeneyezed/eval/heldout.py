"""Held-out scoring after the run (docs/specs/eval-runs.md; master plan: "nobody during the run").

For each closed attempt of a session, check its commit out into a temporary git worktree of the
task workspace (the workspace itself is never touched), score the held-out split there, and upsert
the result into test_evals. Idempotent: attempts already scored are skipped. Only eval/ names the
test_evals collection (invariant I3); the harness never reads it.
"""

from __future__ import annotations

import subprocess
import tempfile
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

COLLECTION = "test_evals"


def _score_heldout(**kwargs: Any) -> dict:
    from tokeneyezed.eval.scorer import score_heldout  # lands with PR #16

    return score_heldout(**kwargs)


def score_session(
    session_id: str,
    attempts: Iterable[dict],
    already_scored: set[str],
    workspace: Path,
    heldout: Path,
    write: Callable[[dict], None],
    program: str = "python3 render.py",
    score_fn: Callable[..., dict] = _score_heldout,
) -> list[dict]:
    """Score every closed, not-yet-scored attempt at its own commit; return the written docs."""
    written = []
    for attempt in sorted(attempts, key=lambda a: a["number"]):
        if attempt.get("status") != "closed" or attempt["attempt_id"] in already_scored:
            continue
        with tempfile.TemporaryDirectory(prefix="heldout-") as tmp:
            tree = Path(tmp) / "tree"
            git = ["git", "-C", str(workspace)]
            subprocess.run(
                [*git, "worktree", "add", "-q", "--detach", str(tree), attempt["commit"]],
                check=True,
                capture_output=True,
            )
            try:
                out = score_fn(heldout=str(heldout), workspace=str(tree), program=program)
            finally:
                subprocess.run(
                    [*git, "worktree", "remove", "--force", str(tree)],
                    check=True,
                    capture_output=True,
                )
        doc = {
            "session_id": session_id,
            "attempt_id": attempt["attempt_id"],
            "number": attempt["number"],
            "agent": attempt.get("agent"),
            "outcome": attempt.get("outcome"),
            "test_pass": out["test_pass"],
            "per_section": out["per_section"],
            "scored_at": datetime.now(UTC),
        }
        write(doc)
        written.append(doc)
    return written


def score_session_in_atlas(
    session_id: str, workspace: Path, heldout: Path, program: str
) -> list[dict]:
    """The CLI path: read attempts from Atlas, upsert into test_evals."""
    from tokeneyezed.data.db import get_db

    db = get_db()
    attempts = list(db.attempts.find({"session_id": session_id}, {"_id": 0}))
    done = {
        d["attempt_id"] for d in db[COLLECTION].find({"session_id": session_id}, {"attempt_id": 1})
    }

    def write(doc: dict) -> None:
        db[COLLECTION].update_one({"attempt_id": doc["attempt_id"]}, {"$set": doc}, upsert=True)

    return score_session(session_id, attempts, done, workspace, heldout, write, program)
