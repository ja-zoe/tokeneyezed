"""Eval-integrity invariants. The catalog and owners are in tests/INVARIANTS.md.

Unbuilt checks skip with the owner's name; `uv run pytest -rs` lists them.
"""

from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "tokeneyezed"
TEST_EVALS_OWNER = SRC / "eval"


def files_mentioning(root: Path, needle: str, allowed: Path) -> list[Path]:
    """Source files under root that contain needle, excluding the allowed subtree."""
    return [
        path
        for path in root.rglob("*.py")
        if allowed not in path.parents and needle in path.read_text(encoding="utf-8")
    ]


def test_i3_scanner_detects_a_planted_reference(tmp_path: Path) -> None:
    # Proves the I3 scanner can see a violation, so its empty result below means something.
    (tmp_path / "eval").mkdir()
    (tmp_path / "eval" / "writer.py").write_text('db["test_evals"]\n')
    (tmp_path / "controller").mkdir()
    leak = tmp_path / "controller" / "brief.py"
    leak.write_text('db["test_evals"].find()\n')

    assert files_mentioning(tmp_path, "test_evals", tmp_path / "eval") == [leak]


def test_i3_harness_never_reads_test_evals() -> None:
    assert SRC.is_dir(), f"source root missing: {SRC}"
    offenders = files_mentioning(SRC, "test_evals", TEST_EVALS_OWNER)
    assert not offenders, f"test_evals referenced outside {TEST_EVALS_OWNER}: {offenders}"


def test_i1_hidden_splits_unreachable_from_task_workspace() -> None:
    pytest.skip("I1 not built yet (owner: Gunjan): needs split + workspace config")


def test_i2_task_workspace_outside_repo() -> None:
    pytest.skip("I2 not built yet (owner: Julian): needs workspace config")


def test_i4_pre_gate_blocks_honeypot() -> None:
    pytest.skip("I4 not built yet (owner: Dharshan): needs pre-gate")


def test_i5_pre_gate_blocks_tampering() -> None:
    pytest.skip("I5 not built yet (owner: Dharshan): needs pre-gate")


def test_i6_hook_configs_outside_task_workspace() -> None:
    pytest.skip("I6 not built yet (owner: Julian): needs runner config")


def test_i7_same_pinned_model_across_runs() -> None:
    pytest.skip("I7 not built yet (owner: Julian): needs run config")


def test_i8_flagged_attempts_stay_out_of_memory() -> None:
    pytest.skip("I8 not built yet (owner: Aaron): needs brief builder + compactor")


def test_i9_shim_fails_closed_pre_and_open_post() -> None:
    pytest.skip("I9 not built yet (owner: Dharshan): needs shim")
