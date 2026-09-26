# Eval-integrity invariants

Properties that must hold no matter what changes. If one breaks, the headline number stops being trustworthy, so each is backed by a check in `test_invariants.py`, run with `uv run pytest`.

A check that can't run yet (its piece isn't built) is **skipped with the owner's name**, never passed. A skip is an unverified claim: `pytest -rs` lists every open one. When you build the piece an invariant depends on, replace the skip with a real check in the same PR.

Every real check must prove it can see a violation (for example, a scanner test that runs against a planted positive), so that a passing result means "no violation" and not "broken check".

| ID | Invariant | Owner | Status |
|---|---|---|---|
| I1 | The validation and held-out splits are unreachable from the task workspace: not inside it, not linked from it, and no split file exists anywhere under it. | Gunjan | skipped: needs split + workspace config |
| I2 | The task workspace lives outside this repo, so attempt agents never load our `AGENTS.md`/`CLAUDE.md`, skills, scorer, or hidden splits. | Julian | **enforced** (the Claude runner refuses to start otherwise) |
| I3 | The harness never reads `test_evals`. The name appears in no source file outside `src/tokeneyezed/eval/`, which owns that collection (writer and indexes). | Gunjan | **enforced** |
| I4 | The pre-gate blocks honeypot shortcuts before they run: installing or importing `markdown-it-py`, `mistune`, or `commonmark`, and copying from `site-packages`. | Dharshan | unit-enforced for recognized commands and source edits/writes; live hook admission pending |
| I5 | The pre-gate blocks edits to the scorer, test files, and hook configs (`.claude/`, `.codex/`). | Dharshan | unit-enforced for direct edits and recognized shell paths; runner isolation and live admission pending |
| I6 | Hook configs and the agents' config dirs (`CLAUDE_CONFIG_DIR`, `CODEX_HOME`) live outside the task workspace. | Julian | **enforced** (the Claude runner refuses to start otherwise) |
| I7 | Runs B, H, and H-mem use the same pinned model, read from one shared config. | Julian | **enforced** (run files may override only name, agent, memory) |
| I8 | A flagged attempt never reaches the brief, embeddings, `memory`, or metric history (it is still written to `attempts` for audit). | Aaron | skipped: needs brief builder + compactor |
| I9 | If the observer is unreachable, the shim blocks in the pre phase (fail closed) and allows in the post phase (fail open). | Dharshan | unit-enforced; live hook admission pending |
