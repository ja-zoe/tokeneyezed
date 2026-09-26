"""End-to-end tests for the eval CLI: real files and renderer subprocesses, no fakes.

Every test drives the real `tokeneyezed` entry point (controller.cli.main) so the
register(subparsers) wiring is exercised, with paths supplied through the same
.env variables operators use (TOKENEYEZED_WORKSPACE, TOKENEYEZED_SPLITS_DIR).
"""

import json
from pathlib import Path

import pytest

from tokeneyezed.controller.cli import main
from tokeneyezed.controller.config import load_config

# A tiny split: two "Tabs" examples and one "Precedence" example.
TABS = [
    {"markdown": "# one\n", "html": "<h1>one</h1>\n", "example": 1, "section": "Tabs"},
    {"markdown": "*two*\n", "html": "<p><em>two</em></p>\n", "example": 2, "section": "Tabs"},
]
PRECEDENCE = [
    {"markdown": "- x\n", "html": "<ul><li>x</li></ul>\n", "example": 3, "section": "Precedence"},
]

# Renderer that answers from an answers.json mapping (markdown -> html), so it
# passes exactly the examples whose expected html is in the mapping.
LOOKUP_RENDERER = """\
import json, sys
md = sys.stdin.read()
sys.stdout.write(json.load(open("answers.json")).get(md, "<p>wrong</p>\\n"))
"""


def set_paths(monkeypatch, tmp_path: Path) -> tuple[Path, Path]:
    """Point the CLI's .env variables at a fresh workspace and hidden splits dir."""
    ws = tmp_path / "workspace"
    hidden = tmp_path / "harness" / "splits"
    ws.mkdir()
    monkeypatch.setenv("TOKENEYEZED_WORKSPACE", str(ws))
    monkeypatch.setenv("TOKENEYEZED_SPLITS_DIR", str(hidden))
    return ws, hidden


def make_workspace(ws: Path, examples: list[dict]) -> None:
    """Drop the lookup renderer and its answers into the workspace."""
    (ws / "render.py").write_text(LOOKUP_RENDERER)
    (ws / "answers.json").write_text(json.dumps({e["markdown"]: e["html"] for e in examples}))


def write_split_files(ws: Path, hidden: Path) -> None:
    """Place tiny visible/validation files where `tokeneyezed split` would put them."""
    (ws / "tests").mkdir()
    (ws / "tests" / "visible.json").write_text(json.dumps(TABS))
    hidden.mkdir(parents=True)
    (hidden / "visible.json").write_text(json.dumps(TABS))  # the copy the scorer reads
    (hidden / "validation.json").write_text(json.dumps([TABS[0]] + PRECEDENCE))


def test_split_writes_visible_inside_and_hidden_outside(monkeypatch, tmp_path, capsys) -> None:
    """`tokeneyezed split` builds 196/234/222 from the env paths (visible in the workspace)."""
    ws, hidden = set_paths(monkeypatch, tmp_path)
    assert main(["split"]) == 0
    assert "visible" in capsys.readouterr().out
    manifest = json.loads((hidden / "manifest.json").read_text())
    assert manifest["seed"] == 20260926
    assert manifest["counts"] == {"visible": 196, "validation": 234, "heldout": 222}
    assert len(json.loads((ws / "tests" / "visible.json").read_text())) == 196
    for name in ("visible.json", "validation.json", "heldout.json"):
        assert (hidden / name).exists()


def test_split_honors_seed_flag(monkeypatch, tmp_path) -> None:
    """--seed reaches the splitter (recorded in the manifest)."""
    _, hidden = set_paths(monkeypatch, tmp_path)
    assert main(["split", "--seed", "7"]) == 0
    assert json.loads((hidden / "manifest.json").read_text())["seed"] == 7


def test_split_refuses_hidden_dir_inside_workspace(monkeypatch, tmp_path) -> None:
    """Invariant I1: a splits dir under the workspace is rejected, nothing is written."""
    ws, _ = set_paths(monkeypatch, tmp_path)
    monkeypatch.setenv("TOKENEYEZED_SPLITS_DIR", str(ws / "splits"))
    with pytest.raises(SystemExit, match="invariant I1"):
        main(["split"])
    assert not (ws / "splits").exists()


def test_split_refuses_workspace_symlink_into_hidden_dir(monkeypatch, tmp_path) -> None:
    """Invariant I1: a symlink inside the workspace resolving to the splits dir is rejected."""
    ws, hidden = set_paths(monkeypatch, tmp_path)
    hidden.mkdir(parents=True)
    (ws / "leak").symlink_to(hidden)
    with pytest.raises(SystemExit, match="symlink.*invariant I1"):
        main(["split"])
    assert not (hidden / "validation.json").exists()


def test_score_attempt_mode_prints_locked_contract(monkeypatch, tmp_path, capsys) -> None:
    """Plain `tokeneyezed score` scores visible + validation in the scorer's attempt mode."""
    ws, hidden = set_paths(monkeypatch, tmp_path)
    write_split_files(ws, hidden)
    make_workspace(ws, TABS + PRECEDENCE)
    assert main(["score"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["visible_pass"] == 1.0 and out["val_pass"] == 1.0
    assert out["per_section"]["Precedence"] == {"visible": None, "val": 1.0}
    assert out["counts"]["visible"]["total"] == 2


@pytest.mark.parametrize(("split_name", "total"), [("visible", 2), ("validation", 2)])
def test_score_one_split(monkeypatch, tmp_path, capsys, split_name, total) -> None:
    """--split visible|validation scores that file alone (the scorer's single mode)."""
    ws, hidden = set_paths(monkeypatch, tmp_path)
    write_split_files(ws, hidden)
    make_workspace(ws, TABS)  # Precedence unanswered -> validation scores 0.5
    assert main(["score", "--split", split_name]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["pass_rate"] == (1.0 if split_name == "visible" else 0.5)
    assert out["counts"]["total"] == total


def test_score_passes_program_and_jobs_through(monkeypatch, tmp_path, capsys) -> None:
    """--program replaces the default `python3 render.py` renderer command."""
    ws, hidden = set_paths(monkeypatch, tmp_path)
    write_split_files(ws, hidden)
    assert main(["score", "--split", "visible", "--program", "cat", "--jobs", "2"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["pass_rate"] == 0.0 and out["counts"]["failed"] == 2


def test_score_ignores_tampered_workspace_visible(monkeypatch, tmp_path, capsys) -> None:
    """An agent-edited tests/visible.json cannot inflate visible_pass.

    Regression for the PR #16 review's finding 5: visible was scored from the
    agent-writable copy. The scorer must read the harness-side copy instead.
    """
    ws, hidden = set_paths(monkeypatch, tmp_path)
    write_split_files(ws, hidden)
    make_workspace(ws, [])  # renderer answers nothing -> every real example fails
    (ws / "tests" / "visible.json").write_text(json.dumps([]))  # "all tests pass now"
    assert main(["score"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["visible_pass"] == 0.0
    assert out["counts"]["visible"]["total"] == 2  # the harness copy, not the empty one


def test_score_without_split_files_says_run_split_first(monkeypatch, tmp_path) -> None:
    ws, _ = set_paths(monkeypatch, tmp_path)
    make_workspace(ws, TABS)
    with pytest.raises(SystemExit, match="run `tokeneyezed split` first"):
        main(["score"])


def test_missing_workspace_env_points_at_env_example(monkeypatch, tmp_path) -> None:
    """An unset TOKENEYEZED_WORKSPACE is a clear setup error, not a traceback."""
    monkeypatch.setenv("TOKENEYEZED_WORKSPACE", "")
    monkeypatch.setenv("TOKENEYEZED_SPLITS_DIR", str(tmp_path / "splits"))
    with pytest.raises(SystemExit, match="TOKENEYEZED_WORKSPACE.*\\.env"):
        main(["split"])


def test_baseline_stub_reads_budget_from_config(monkeypatch, tmp_path) -> None:
    """`baseline` loads configs/b.toml (extending base.toml); the loop lands in STEP 3."""
    set_paths(monkeypatch, tmp_path)
    config = load_config("configs/b.toml")  # the values live in TOML, never in code
    with pytest.raises(SystemExit) as exc:
        main(["baseline", "--config", "configs/b.toml"])
    message = str(exc.value)
    assert "STEP 3" in message
    assert f"{config.max_attempts} attempts x {config.max_turns} turns" in message
    assert f"run {config.name}" in message


def test_report_stub_names_sessions(monkeypatch, tmp_path) -> None:
    set_paths(monkeypatch, tmp_path)
    with pytest.raises(SystemExit, match="STEP 5.*B-1 H-1"):
        main(["report", "--sessions", "B-1", "H-1"])
