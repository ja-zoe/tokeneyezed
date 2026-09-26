"""End-to-end tests for the scorer: real renderer subprocesses, no fakes or mocks.

Each test builds a real task workspace with a small render.py variant and real
split files, then drives the actual CLI (main) and checks the locked output
contract (scorer-output field names must match Aaron's `attempts` schema).
"""

import json
from pathlib import Path

import pytest

from tokeneyezed.eval.scorer import SCORER_VERSION, SPEC_VERSION, main

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


def make_workspace(tmp_path: Path, examples: list[dict], renderer: str = LOOKUP_RENDERER) -> Path:
    """Create a real task workspace with render.py and the answers mapping."""
    ws = tmp_path / "workspace"
    ws.mkdir(exist_ok=True)
    (ws / "render.py").write_text(renderer)
    (ws / "answers.json").write_text(json.dumps({e["markdown"]: e["html"] for e in examples}))
    return ws


def write_split(tmp_path: Path, name: str, examples: list[dict]) -> str:
    path = tmp_path / name
    path.write_text(json.dumps(examples))
    return str(path)


def run_cli(capsys, argv: list[str]) -> dict:
    """Run the real scorer CLI and parse the JSON it prints."""
    assert main(argv) == 0
    return json.loads(capsys.readouterr().out)


def test_attempt_mode_contract(tmp_path: Path, capsys) -> None:
    """Attempt mode emits exactly the locked fields; missing-split sections are null."""
    ws = make_workspace(tmp_path, TABS + PRECEDENCE)
    vis = write_split(tmp_path, "visible.json", TABS)
    val = write_split(tmp_path, "validation.json", [TABS[0]] + PRECEDENCE)
    out = run_cli(
        capsys, ["attempt", "--visible", vis, "--validation", val, "--workspace", str(ws)]
    )
    assert list(out) == [
        "scorer_version",
        "spec_version",
        "visible_pass",
        "val_pass",
        "per_section",
        "counts",
        "duration_s",
    ]
    assert out["scorer_version"] == SCORER_VERSION
    assert out["spec_version"] == SPEC_VERSION
    assert out["visible_pass"] == 1.0 and out["val_pass"] == 1.0
    # Precedence has no visible examples -> visible is null (tiny-section rule).
    assert out["per_section"]["Precedence"] == {"visible": None, "val": 1.0}
    assert out["per_section"]["Tabs"] == {"visible": 1.0, "val": 1.0}
    assert out["counts"]["visible"] == {
        "total": 2,
        "passed": 2,
        "failed": 0,
        "errors": 0,
        "timeouts": 0,
    }
    assert out["counts"]["validation"]["total"] == 2


def test_heldout_mode_emits_test_pass_only(tmp_path: Path, capsys) -> None:
    """Heldout mode reports test_pass (for test_evals) and never visible/val fields."""
    ws = make_workspace(tmp_path, TABS)  # Precedence missing from answers -> fails
    held = write_split(tmp_path, "heldout.json", TABS + PRECEDENCE)
    out = run_cli(capsys, ["heldout", "--heldout", held, "--workspace", str(ws)])
    assert out["test_pass"] == round(2 / 3, 4)
    assert "visible_pass" not in out and "val_pass" not in out and "pass_rate" not in out
    assert out["per_section"] == {"Precedence": 0.0, "Tabs": 1.0}
    assert out["counts"] == {"total": 3, "passed": 2, "failed": 1, "errors": 0, "timeouts": 0}


def test_single_mode_and_trailing_newline_tolerance(tmp_path: Path, capsys) -> None:
    """Single mode scores one file; a trailing-newline-only diff still passes."""
    stripped = [{**e, "html": e["html"].rstrip("\n")} for e in TABS]  # renderer adds "\n"
    ws = make_workspace(tmp_path, TABS)
    split = write_split(tmp_path, "any.json", stripped)
    out = run_cli(capsys, ["single", "--file", split, "--workspace", str(ws)])
    assert out["pass_rate"] == 1.0
    assert out["counts"]["passed"] == 2


def test_empty_split_scores_zero(tmp_path: Path, capsys) -> None:
    """An empty split file yields rate 0.0, not a division crash."""
    ws = make_workspace(tmp_path, [])
    split = write_split(tmp_path, "empty.json", [])
    out = run_cli(capsys, ["single", "--file", split, "--workspace", str(ws)])
    assert out["pass_rate"] == 0.0
    assert out["counts"]["total"] == 0 and out["per_section"] == {}


def test_crashing_renderer_counts_as_error(tmp_path: Path, capsys) -> None:
    """Non-zero exit is an error, never a pass (even if stdout matched first)."""
    ws = make_workspace(tmp_path, TABS, renderer="import sys\nsys.exit(3)\n")
    split = write_split(tmp_path, "s.json", TABS)
    out = run_cli(capsys, ["single", "--file", split, "--workspace", str(ws)])
    assert out["pass_rate"] == 0.0
    assert out["counts"]["errors"] == 2


def test_unlaunchable_program_counts_as_error(tmp_path: Path, capsys) -> None:
    """A renderer command that cannot start (OSError) is an error, not a crash."""
    ws = make_workspace(tmp_path, TABS)
    split = write_split(tmp_path, "s.json", TABS[:1])
    out = run_cli(
        capsys,
        [
            "single",
            "--file",
            split,
            "--workspace",
            str(ws),
            "--program",
            str(tmp_path / "no-such-renderer"),
        ],
    )
    assert out["counts"] == {"total": 1, "passed": 0, "failed": 0, "errors": 1, "timeouts": 0}


def test_slow_renderer_times_out(tmp_path: Path, capsys) -> None:
    """A renderer exceeding --timeout is a timeout, never a pass."""
    ws = make_workspace(tmp_path, TABS, renderer="import time\ntime.sleep(5)\n")
    split = write_split(tmp_path, "s.json", TABS[:1])
    out = run_cli(capsys, ["single", "--file", split, "--workspace", str(ws), "--timeout", "0.3"])
    assert out["counts"] == {"total": 1, "passed": 0, "failed": 0, "errors": 0, "timeouts": 1}
    assert out["pass_rate"] == 0.0


@pytest.mark.parametrize(
    "argv",
    [
        ["attempt", "--visible", "v.json"],  # missing --validation
        ["heldout"],  # missing --heldout
        ["single"],  # missing --file
    ],
)
def test_missing_mode_arguments_exit(tmp_path: Path, argv: list[str]) -> None:
    """Each mode rejects a call without its required split file (argparse exit 2)."""
    with pytest.raises(SystemExit) as exc:
        main([*argv, "--workspace", str(tmp_path)])
    assert exc.value.code == 2
