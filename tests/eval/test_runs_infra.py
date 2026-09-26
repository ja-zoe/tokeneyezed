"""Task workspace, SpecScorer, and held-out scoring (docs/specs/eval-runs.md)."""

import json
import subprocess
from pathlib import Path

import pytest

from tokeneyezed.eval.heldout import score_session
from tokeneyezed.eval.scoring import SpecScorer
from tokeneyezed.eval.workspace import RENDER_COMMAND, init_workspace

HARNESS = Path(__file__).resolve().parents[2]
VISIBLE = [
    {"example": 1, "section": "Tabs", "markdown": "a\n", "html": "<p>a</p>\n"},
    {"example": 2, "section": "Links", "markdown": "b\n", "html": "<p>b</p>\n"},
]


def git(ws, *args):
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t.invalid", *args],
        cwd=ws,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture
def workspace(tmp_path):
    split = tmp_path / "visible.json"
    split.write_text(json.dumps(VISIBLE))
    return init_workspace(tmp_path / "ws", split)


def test_workspace_init(workspace):
    assert {p.name for p in workspace.iterdir()} >= {
        ".git",
        ".gitignore",
        "README.md",
        "render.py",
        "run_visible.py",
        "tests",
    }
    assert json.loads((workspace / "tests" / "visible.json").read_text()) == VISIBLE
    assert RENDER_COMMAND in (workspace / "README.md").read_text()
    assert git(workspace, "log", "--oneline").count("\n") == 0  # exactly one commit
    assert git(workspace, "status", "--porcelain") == ""
    # The stub honors the contract: exit 0, empty output.
    stub = subprocess.run(
        RENDER_COMMAND.split(), input="# x\n", cwd=workspace, capture_output=True, text=True
    )
    assert stub.returncode == 0 and stub.stdout == ""
    # The self-test script runs against the stub and reports 0 of 2.
    report = subprocess.run(
        ["python3", "run_visible.py"], cwd=workspace, capture_output=True, text=True
    )
    assert report.returncode == 0 and "visible: 0/2 passed" in report.stdout


def test_workspace_init_refuses_unsafe_locations(tmp_path):
    split = tmp_path / "visible.json"
    split.write_text(json.dumps(VISIBLE))
    with pytest.raises(SystemExit, match="I2"):
        init_workspace(HARNESS / "task-ws", split)
    (tmp_path / "full").mkdir()
    (tmp_path / "full" / "x").write_text("x")
    with pytest.raises(SystemExit, match="not empty"):
        init_workspace(tmp_path / "full", split)


def test_spec_scorer_maps_output_and_drops_missing_visible(tmp_path):
    calls = []

    def fake_score_attempt(**kwargs):
        calls.append(kwargs)
        return {
            "visible_pass": 0.5,
            "val_pass": 0.25,
            "per_section": {
                "Tabs": {"visible": 1.0, "val": 0.5},
                "Precedence": {"visible": None, "val": 0.0},
            },
        }

    scorer = SpecScorer(
        workspace=tmp_path / "ws",
        visible=tmp_path / "hidden" / "visible.json",
        validation=tmp_path / "hidden" / "validation.json",
        score_fn=fake_score_attempt,
    )
    score = scorer.score()
    assert (score.visible_pass, score.val_pass) == (0.5, 0.25)
    assert score.per_section == {"Tabs": {"visible": 1.0, "val": 0.5}, "Precedence": {"val": 0.0}}
    assert score.section_val("Precedence") == 0.0
    # Visible is scored from the harness-side copy, never the workspace's.
    assert calls[0]["visible"] == str(tmp_path / "hidden" / "visible.json")
    assert calls[0]["workspace"] == str(tmp_path / "ws")


def test_heldout_scores_each_attempt_at_its_own_commit(workspace, tmp_path):
    (workspace / "render.py").write_text("# version one\n")
    git(workspace, "commit", "-qam", "attempt 1")
    first = git(workspace, "rev-parse", "HEAD")
    (workspace / "render.py").write_text("# version two\n")
    git(workspace, "commit", "-qam", "attempt 2")
    second = git(workspace, "rev-parse", "HEAD")
    (workspace / "scratch.py").write_text("the live workspace's uncommitted work\n")

    def fake_score_heldout(heldout, workspace, program):
        # Score depends on the checked-out render.py, so a wrong checkout gives the wrong score.
        version = Path(workspace, "render.py").read_text()
        return {"test_pass": 0.9 if "two" in version else 0.4, "per_section": {"Tabs": 0.5}}

    attempts = [
        {
            "attempt_id": "s-002",
            "number": 2,
            "commit": second,
            "status": "closed",
            "agent": "codex",
            "outcome": "improved",
        },
        {
            "attempt_id": "s-001",
            "number": 1,
            "commit": first,
            "status": "closed",
            "agent": "claude",
            "outcome": "improved",
        },
        {"attempt_id": "s-003-x", "number": 3, "status": "killed"},
        {"attempt_id": "s-000", "number": 0, "commit": first, "status": "closed"},
    ]
    written = []
    docs = score_session(
        "s",
        attempts,
        {"s-000"},
        workspace,
        tmp_path / "heldout.json",
        written.append,
        score_fn=fake_score_heldout,
    )

    assert [(d["attempt_id"], d["test_pass"]) for d in docs] == [("s-001", 0.4), ("s-002", 0.9)]
    assert written == docs  # killed and already-scored attempts are skipped
    assert docs[0]["agent"] == "claude" and docs[1]["number"] == 2
    # The live workspace is untouched, and no worktrees are left behind.
    assert git(workspace, "rev-parse", "HEAD") == second
    assert (workspace / "scratch.py").exists()
    assert git(workspace, "worktree", "list").count("\n") == 0
