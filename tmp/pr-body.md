## What

Ports the eval lane's STEP 0: the CommonMark 0.31.2 spec data, the three-way example splitter, and the renderer scorer — plus end-to-end tests for both modules.

## Why

This is the foundation the rest of the eval/demo lane builds on: the split defines what the agent can see vs. what the harness holds back, and the scorer produces the JSON that Julian's attempt runner merges into `attempts` and that `test_evals` reports from. It is fully decoupled work — nothing here waits on the data or agent-loop lanes.

## Contents

**`src/tokeneyezed/eval/data/spec-0.31.2.json`** — all 652 CommonMark spec examples (markdown, expected HTML, section), vendored so runs are hermetic.

**`src/tokeneyezed/eval/split.py`** — stratified split of the spec examples into three sets, deterministic by seed (default `20260926`):

| split | share | goes to |
|---|---|---|
| visible | ~30% | agent's task workspace (the only tests it sees) |
| validation | ~35% | harness-only; drives replans and goal completion |
| heldout | ~35% | untouched during runs; the final reported number (`test_evals`) |

- Stratified per spec section; tiny sections (1–3 examples) are allocated by priority validation → heldout → visible, so per-section coverage of goals is never lost.
- Writes `manifest.json` (seed, spec sha256, per-section example ids) for reproducibility.
- Enforces invariant **I1**: `write_splits()` refuses to place validation/heldout files anywhere reachable from the visible destination's directory tree.

**`src/tokeneyezed/eval/scorer.py`** — scores any renderer against a split. Renderer contract is agent-agnostic: a command that reads Markdown on stdin and writes HTML on stdout (default `python3 render.py`), run in the task workspace. Exact-match comparison tolerating only a trailing newline; non-zero exit, timeout, or mismatch all fail.

Three modes, emitting the JSON shapes locked in the 10:45 contract sync (field names must not change):
- `attempt` — visible + validation pass rates with per-section breakdown; merged straight into `attempts`.
- `heldout` — held-out pass rate; goes **only** to `test_evals` (invariant I3).
- `single` — score any one split file, for debugging.

Examples run concurrently (`--jobs`), each with a 5 s default timeout.

## Tests

`tests/eval/test_split.py` + `tests/eval/test_scorer.py` — end-to-end (real subprocess renderers, real files, no mocks): split determinism per seed, share and stratification guarantees, tiny-section priority allocation, the I1 path check, manifest integrity, and scorer pass/fail/error/timeout paths plus exact output-shape checks for all three modes.

Full suite on this branch: **276 passed, 2 skipped**; `ruff check` clean.

## Also in this PR

`docs/work-split.md` — replaces Gunjan's work-split prompt block with the v2 eval-lane prompt (the version that matches what this PR implements). Rebased onto current main, so Julian's/Dharshan's "agent handoff" rewordings are preserved.
