"""End-to-end tests for the spec split: real spec data, real files, no fakes.

The two `assert` statements inside write_splits() (total preserved, no example in
two splits) are internal sanity checks that cannot fire without corrupting
split_examples() itself, so they are not exercised here.
"""

import hashlib
import json
import tomllib
from pathlib import Path

import pytest

from tokeneyezed.eval.split import DEFAULT_SEED, DEFAULT_SPEC, main, split_examples

REPO = Path(__file__).resolve().parents[2]
EXPECTED_COUNTS = {"visible": 196, "validation": 234, "heldout": 222}
VAL_ONLY_SECTIONS = {"Precedence", "Blank lines", "Inlines"}  # 1 example each


def run_split(tmp_path: Path) -> tuple[Path, Path]:
    """Run the real CLI against the bundled spec; return (hidden_dir, visible_dest)."""
    hidden = tmp_path / "harness" / "splits"
    visible = tmp_path / "workspace" / "tests" / "visible.json"
    assert main(["--hidden-dir", str(hidden), "--visible-dest", str(visible)]) == 0
    return hidden, visible


def test_split_counts_and_manifest(tmp_path: Path) -> None:
    """Seed 20260926 yields 196/234/222 and a manifest with the spec's sha256."""
    hidden, visible = run_split(tmp_path)
    manifest = json.loads((hidden / "manifest.json").read_text())
    assert manifest["seed"] == DEFAULT_SEED == 20260926
    assert manifest["counts"] == EXPECTED_COUNTS
    assert manifest["spec_version"] == "0.31.2"
    assert manifest["spec_sha256"] == hashlib.sha256(DEFAULT_SPEC.read_bytes()).hexdigest()
    for split_name, path in [
        ("visible", visible),
        ("validation", hidden / "validation.json"),
        ("heldout", hidden / "heldout.json"),
    ]:
        assert len(json.loads(path.read_text())) == EXPECTED_COUNTS[split_name]


def test_split_is_a_partition_of_the_spec(tmp_path: Path) -> None:
    """Every one of the 652 examples lands in exactly one split."""
    hidden, visible = run_split(tmp_path)
    spec_ids = {e["example"] for e in json.loads(DEFAULT_SPEC.read_text())}
    seen: list[int] = []
    for path in (visible, hidden / "validation.json", hidden / "heldout.json"):
        seen.extend(e["example"] for e in json.loads(path.read_text()))
    assert len(seen) == len(spec_ids) == 652
    assert set(seen) == spec_ids


def test_tiny_section_priority(tmp_path: Path) -> None:
    """1-example sections are val-only; the 2-example section is val + heldout."""
    hidden, _ = run_split(tmp_path)
    sections = json.loads((hidden / "manifest.json").read_text())["sections"]
    for s in VAL_ONLY_SECTIONS:
        assert sections[s]["visible"] == [] and sections[s]["heldout"] == []
        assert len(sections[s]["validation"]) == 1
    soft = sections["Soft line breaks"]
    assert soft["visible"] == []
    assert len(soft["validation"]) == 1 and len(soft["heldout"]) == 1


def test_sections_match_base_toml(tmp_path: Path) -> None:
    """The manifest's section names equal the goal sections in configs/base.toml."""
    hidden, _ = run_split(tmp_path)
    manifest = json.loads((hidden / "manifest.json").read_text())
    base = tomllib.loads((REPO / "configs" / "base.toml").read_text())
    assert set(manifest["sections"]) == set(base["sections"])
    assert len(base["sections"]) == 26


def test_split_is_deterministic(tmp_path: Path) -> None:
    """Two runs with the default seed produce byte-identical outputs."""
    h1, v1 = run_split(tmp_path / "a")
    h2, v2 = run_split(tmp_path / "b")
    assert v1.read_bytes() == v2.read_bytes()
    for name in ("validation.json", "heldout.json", "manifest.json"):
        assert (h1 / name).read_bytes() == (h2 / name).read_bytes()


def test_different_seed_changes_membership_not_counts() -> None:
    """Seeds shuffle membership within sections but never the per-split counts."""
    examples = json.loads(DEFAULT_SPEC.read_text())
    a = split_examples(examples, DEFAULT_SEED)
    b = split_examples(examples, DEFAULT_SEED + 1)
    assert {s: len(v) for s, v in a.items()} == {s: len(v) for s, v in b.items()}
    assert [e["example"] for e in a["visible"]] != [e["example"] for e in b["visible"]]


def test_i1_guard_hidden_dir_under_workspace(tmp_path: Path) -> None:
    """Refuses a hidden dir nested inside the task workspace (invariant I1)."""
    visible = tmp_path / "workspace" / "tests" / "visible.json"
    hidden = tmp_path / "workspace" / "tests" / "splits"
    with pytest.raises(SystemExit, match="invariant I1"):
        main(["--hidden-dir", str(hidden), "--visible-dest", str(visible)])


def test_i1_guard_workspace_under_hidden_dir(tmp_path: Path) -> None:
    """Refuses a workspace nested inside the hidden dir (invariant I1, other way)."""
    hidden = tmp_path / "splits"
    visible = tmp_path / "splits" / "deeper" / "workspace" / "visible.json"
    with pytest.raises(SystemExit, match="invariant I1"):
        main(["--hidden-dir", str(hidden), "--visible-dest", str(visible)])
