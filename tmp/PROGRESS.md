# STEP 1 — eval CLI (`tokeneyezed split/score/baseline/report`)

Task: create `src/tokeneyezed/eval/cli.py` exposing `register(subparsers)`, wire it into
`controller/cli.py` with one call, per the docs/commands.md convention. Paths from `.env`,
shared values from `configs/base.toml` (never hardcoded).

## Done so far

1. **Recalled context** from memory (STEP 0 already ported: `eval/{split,scorer}.py` + spec data
   on branch `eval/pipeline`, PR #16) and read `docs/work-split.md` STEP 1 spec,
   `docs/commands.md` convention, `controller/cli.py`, `controller/config.py`, `.env.example`.
2. **Wrote `src/tokeneyezed/eval/cli.py`** (the only new module):
   - `workspace_dir()` / `splits_dir()` read `TOKENEYEZED_WORKSPACE` (required) and
     `TOKENEYEZED_SPLITS_DIR` (default `~/.tokeneyezed/splits`) from the environment,
     with a "copy .env.example" exit message when unset.
   - `split [--seed N]` delegates to `split.main` with the env paths. Added a CLI-level
     invariant-I1 guard: `split.write_splits` only protects the visible file's parent
     (`ws/tests`), so a hidden dir at `ws/splits` would have slipped through — the CLI
     refuses any splits dir reachable from the workspace root:
     ```python
     if hidden.is_relative_to(ws) or ws.is_relative_to(hidden):
         sys.exit("refusing: ... (invariant I1)")
     ```
   - `score [--split visible|validation] [--program ...] [--jobs N]` delegates to
     `scorer.main`: attempt mode (visible + validation) by default, single mode with
     `--split`; missing split files exit with "run `tokeneyezed split` first".
   - `baseline --config configs/b.toml` loads the run config via `load_config` and exits
     with a STEP 3 stub message echoing `max_attempts x max_turns` from the TOML
     (proves nothing is hardcoded). `report [--sessions ...]` is the STEP 5 stub.
3. **Wiring** in `controller/cli.py`: one import + `register_eval(sub)` before `parse_args`.
4. **`.env.example`**: documented `TOKENEYEZED_SPLITS_DIR` next to `TOKENEYEZED_WORKSPACE`.
5. **Tests** `tests/eval/test_eval_cli.py` (renamed from test_cli.py — basename collided with
   `tests/controller/test_cli.py`, no `__init__.py` in test dirs): 10 end-to-end tests driving
   the real `tokeneyezed` entry point with real files and renderer subprocesses — split counts
   (196/234/222) + manifest seed, `--seed` pass-through, I1 refusal, score attempt contract,
   single-split scores, `--program`/`--jobs` pass-through, missing-files hint, unset-env error,
   baseline stub echoing config values, report stub naming sessions.
6. **Full suite** (289 tests) run in 10 parallel splits: 287 passed, 2 skipped (the
   pre-existing invariant skips). Note: split-file test ids contain spaces (parametrized ids),
   so the parallel runner feeds pytest via `tr '\n' '\0' | xargs -0`.
7. **docs/commands.md**: split/score flipped to ✅, baseline to "🔨 CLI wired; loop 📋".

## Review round (gpt-5.6-sol, read-only)

Reviewer confirmed wiring, flags, stubs, no hardcoded config values, no I3 violations,
47 tests passing — and demonstrated 2 real issues, both fixed:

1. **I1 symlink bypass (high)**: a pre-existing symlink inside the workspace pointing at the
   hidden splits dir made validation/heldout reachable after `tokeneyezed split`. Fix:
   `_leaking_symlink()` walks the workspace (os.walk, not following links) and refuses any
   symlink whose resolution reaches the hidden dir. New test:
   `test_split_refuses_workspace_symlink_into_hidden_dir`.
2. **E501** at cli.py:81 (102 chars) — wrapped.

## Final verification

- `uv run ruff check`: clean. `uv run ruff format`: my files formatted, plus the three
  STEP 0 eval-lane stragglers (scorer.py, split.py, test_scorer.py) so the repo-wide
  format check CI runs is green (66 files, all formatted).
- `uv run pytest tests/eval tests/controller -q`: 48 passed; earlier full suite
  (289 tests, 10 parallel splits): 287 passed, 2 pre-existing invariant skips.
- New files staged: `src/tokeneyezed/eval/cli.py`, `tests/eval/test_eval_cli.py`.
