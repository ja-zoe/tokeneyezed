"""Split the CommonMark spec examples into visible / validation / heldout sets.

Stratified by spec section so every section is represented in all three splits
whenever it has enough examples:

  visible    ~30%  -> written into the agent's task workspace (the only tests it sees)
  validation ~35%  -> harness-only; drives replans and goal completion
  heldout    ~35%  -> untouched during runs; the final reported number (test_evals)

Tiny sections (the spec has sections with 1-3 examples) are allocated by priority:
validation first (goals are keyed on per-section val pass), then heldout, then
visible. So a 1-example section goes entirely to validation and a 2-example
section to validation + heldout — which is why `per_section.<s>.visible` can be
null in the scorer output.

Invariant I1: the validation and heldout files must be unreachable from the task
workspace. write_splits() refuses to place them inside the visible destination's
directory tree (or vice versa).

Usage:
  python3 -m tokeneyezed.eval.split --hidden-dir /path/harness/splits \
      --visible-dest /path/task-workspace/tests/visible.json [--seed 20260926]

Outputs (all lists of spec.json-shaped objects):
  <hidden-dir>/validation.json
  <hidden-dir>/heldout.json
  <hidden-dir>/manifest.json   seed, spec sha256, per-section example ids per split
  <visible-dest>               the visible split
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
from collections import defaultdict
from collections.abc import Sequence
from pathlib import Path

DEFAULT_SPEC = Path(__file__).parent / "data" / "spec-0.31.2.json"
DEFAULT_SEED = 20260926
SHARES = {"visible": 0.30, "validation": 0.35, "heldout": 0.35}
# Allocation priority for sections too small to cover all three splits.
PRIORITY = ["validation", "heldout", "visible"]


def allocate_counts(n: int) -> dict[str, int]:
    """Return how many of a section's n examples go to each split.

    Guarantees each split gets at least one example when n >= 3; for n < 3,
    fills splits in PRIORITY order. Remaining examples are distributed by
    largest remainder against the target SHARES.
    """
    counts = dict.fromkeys(SHARES, 0)
    if n < 3:
        for i in range(n):
            counts[PRIORITY[i]] += 1
        return counts
    for split in SHARES:
        counts[split] = 1
    remaining = n - 3
    quotas = {s: remaining * SHARES[s] for s in SHARES}
    for s in SHARES:
        counts[s] += int(quotas[s])
    leftover = n - sum(counts.values())
    by_remainder = sorted(SHARES, key=lambda s: quotas[s] - int(quotas[s]), reverse=True)
    for i in range(leftover):
        counts[by_remainder[i % len(by_remainder)]] += 1
    return counts


def split_examples(examples: list[dict], seed: int) -> dict[str, list[dict]]:
    """Deterministically split spec examples into the three sets, stratified by section."""
    by_section: dict[str, list[dict]] = defaultdict(list)
    for ex in examples:
        by_section[ex["section"]].append(ex)
    rng = random.Random(seed)
    splits: dict[str, list[dict]] = {s: [] for s in SHARES}
    for section in sorted(by_section):
        exs = sorted(by_section[section], key=lambda e: e["example"])
        rng.shuffle(exs)
        counts = allocate_counts(len(exs))
        pos = 0
        for split_name in ("visible", "validation", "heldout"):
            take = counts[split_name]
            splits[split_name].extend(exs[pos : pos + take])
            pos += take
    for s in splits:
        splits[s].sort(key=lambda e: e["example"])
    return splits


def write_splits(
    spec: Path, hidden_dir: Path, visible_dest: Path, seed: int, spec_version: str
) -> dict:
    """Split the spec and write the four output files; return the manifest.

    Raises SystemExit when the hidden dir and the task workspace (the visible
    destination's directory) are reachable from each other (invariant I1).
    """
    hidden_dir = hidden_dir.resolve()
    visible_dest = visible_dest.resolve()
    workspace = visible_dest.parent
    # Invariant I1: hidden splits must not live under the task workspace tree.
    if hidden_dir.is_relative_to(workspace) or any(p == hidden_dir for p in workspace.parents):
        raise SystemExit(
            f"refusing: hidden dir {hidden_dir} is reachable from the task "
            f"workspace {workspace} (invariant I1)"
        )

    spec_bytes = spec.read_bytes()
    examples = json.loads(spec_bytes)
    splits = split_examples(examples, seed)

    total = sum(len(v) for v in splits.values())
    assert total == len(examples), "every example must land in exactly one split"
    seen = [e["example"] for v in splits.values() for e in v]
    assert len(set(seen)) == total, "no example may appear in two splits"

    hidden_dir.mkdir(parents=True, exist_ok=True)
    visible_dest.parent.mkdir(parents=True, exist_ok=True)
    visible_dest.write_text(json.dumps(splits["visible"], indent=1) + "\n")
    (hidden_dir / "validation.json").write_text(json.dumps(splits["validation"], indent=1) + "\n")
    (hidden_dir / "heldout.json").write_text(json.dumps(splits["heldout"], indent=1) + "\n")

    manifest = {
        "seed": seed,
        "spec_version": spec_version,
        "spec_sha256": hashlib.sha256(spec_bytes).hexdigest(),
        "counts": {s: len(v) for s, v in splits.items()},
        "sections": {
            section: {
                s: sorted(e["example"] for e in splits[s] if e["section"] == section)
                for s in splits
            }
            for section in sorted({e["section"] for e in examples})
        },
    }
    (hidden_dir / "manifest.json").write_text(json.dumps(manifest, indent=1) + "\n")
    return manifest


def main(argv: Sequence[str] | None = None) -> int:
    """Parse args, produce the three split files and a manifest, print a summary."""
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument(
        "--spec", type=Path, default=DEFAULT_SPEC, help="path to the official spec.json"
    )
    ap.add_argument(
        "--hidden-dir",
        type=Path,
        required=True,
        help="harness-side dir for validation.json, heldout.json, manifest.json",
    )
    ap.add_argument(
        "--visible-dest",
        type=Path,
        required=True,
        help="file path inside the task workspace for the visible split",
    )
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED)
    ap.add_argument("--spec-version", default="0.31.2")
    args = ap.parse_args(argv)

    manifest = write_splits(
        args.spec, args.hidden_dir, args.visible_dest, args.seed, args.spec_version
    )
    total = sum(manifest["counts"].values())
    print(f"split {total} examples (seed {args.seed}):")
    for s, n in manifest["counts"].items():
        print(f"  {s:<10} {n:>3}  ({n / total:.1%})")
    print(f"visible  -> {args.visible_dest.resolve()}")
    print(f"hidden   -> {args.hidden_dir.resolve()}/{{validation,heldout,manifest}}.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
