# Spec: task workspace, Scorer adapter, and held-out scoring after the run

**Status:** approved 2026-09-26 · **Owner:** Julian (taking over from Gunjan's eval lane, in parallel with his PR #16 fixes) · **Branch:** `eval/runs-infra`

## Problem

No real run can start: there is no task workspace for the agent, and nothing implements the `Scorer` port over Gunjan's scorer. And nothing produces the headline number: held-out scores per attempt into `test_evals` (open question 5: who writes it).

This branch does not touch `eval/split.py` or `eval/scorer.py` (Gunjan is fixing them in #16); it builds on their public functions.

## Approach

### 1. Task workspace: `tokeneyezed workspace init DIR` (`eval/workspace.py`)

Creates the repo the agent works in, **outside this harness repo** (I2), as a git repo with one initial commit:
- `README.md`: the task (a CommonMark renderer in Python, from scratch; no Markdown libraries), and **the renderer contract the scorer uses: `python3 render.py` reads Markdown on stdin and writes HTML on stdout**, plus how to run the visible tests.
- `render.py`: a stub that honors the contract (reads stdin, writes nothing), so attempt 1 scores 0 instead of erroring.
- `tests/visible.json` (the visible split) and `run_visible.py` (a tiny self-test script so the agent can check itself).
- `.gitignore` for `__pycache__/` and `*.pyc` (the harness commits everything).

The hidden splits (validation, held-out, and a harness-side copy of visible) go to a separate `--hidden-dir` outside the workspace, via Gunjan's split module once #16 lands. Until then `workspace init` refuses to run without it.

### 2. `SpecScorer` (`eval/scoring.py`), implementing the `Scorer` port

`score()` calls `scorer.score_attempt` with the **harness-side** visible copy and the validation file, the workspace, and the renderer program, and maps the result to `Score`. A section with no visible examples comes back as `visible: None` (tiny sections); the port's `Score` documents that, and the `Scorer` contract test allows it.

### 3. Held-out scoring after the run: `tokeneyezed heldout SESSION` (`eval/heldout.py`)

The master plan says held-out is used by "nobody during the run", so it is scored afterwards, per attempt:
- Read the session's closed attempts (their `number`, `commit`, `outcome`) from `attempts`.
- For each commit, check it out into a temporary `git worktree` of the task workspace (never touching the workspace itself, so it can run while the session continues), score the held-out split with `scorer.score_heldout`, and upsert `{session_id, attempt_id, number, outcome, test_pass, per_section, scored_at}` into `test_evals`.
- Idempotent: already-scored attempts are skipped, so it can be re-run as the session progresses.
- Only `eval/` names `test_evals` (I3); the harness never reads it.

This answers open question 5: the eval package writes `test_evals`, after the fact, and nothing in the run's path does.

### Out of scope

The baseline runner B (next, once this lands), the report and chart, and the fixes in #16.

## Tests (the merge gate)

- [x] `workspace init` creates a git repo with the README contract, a stub that the scorer runs without error, the visible split, the self-test script, and the `.gitignore`; it refuses a directory inside the harness repo.
- [x] `SpecScorer` maps scorer output to `Score` (including `visible: None`), scores visible from the harness-side copy, and passes the `Scorer` contract test.
- [x] `heldout` scores each closed attempt at its own commit (proven with a renderer that improves across two commits), skips killed attempts and already-scored ones, leaves the workspace untouched, and writes only to `test_evals`.
- [x] I3 still holds: `test_evals` appears only under `eval/`.
- [x] `uv run pytest -rs`, `uv run ruff check`, `uv run ruff format --check` pass.

**Live check (done 2026-09-26, passed with the real scorer from #16):** `workspace init` against the real spec split, `SpecScorer` on the stub (all zeros, no errors), and `heldout` on a two-attempt session in the smoke database.

Ship: PR from this branch, squash-merged after Julian approves.
