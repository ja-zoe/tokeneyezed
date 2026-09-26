# Command interface

Every command we should be able to run, who owns it, and whether it exists yet. One entry point, `tokeneyezed`, with a subcommand per job, so the demo and the day's runs never depend on remembering `python -m` paths.

**Status key:** ✅ exists on `main` · 🔨 in progress · 📋 planned (owner builds it) · ⚠️ blocking something

**Convention for adding a subcommand:** each workstream keeps its commands in its own package (e.g. `eval/cli.py`) and exposes `register(subparsers)`; `controller/cli.py` calls it. That keeps four people out of one file. Machine-specific paths come from `.env` (see `.env.example`); shared behavior comes from `configs/`.

## Runs (controller, Julian)

| Command | What it does | Status |
|---|---|---|
| `tokeneyezed run --config configs/h.toml [--session-id ID]` | Start a session and stream the live feed until done or killed (Ctrl-C). Configs: `b.toml`, `h.toml`, `h-mem.toml`. Records the `sessions` document. | ✅ |
| `tokeneyezed run ... --fake [--checkpointer memory]` | The whole loop on in-memory fakes, for trying things without Atlas or an agent. | ✅ |
| `tokeneyezed resume ID --config configs/h.toml [--agent AGENT]` | Continue a session from its Atlas checkpoint: marks the killed attempt, resets the workspace, prints the `RESUMED` banner, and carries on, optionally on a different agent (the agent handoff). | ✅ |
| `tokeneyezed status ID` | Session progress from its checkpoint: attempts, current goal, best score per goal. | ✅ (needs `MONGODB_URI`) |
| `tokeneyezed attempt --config configs/h.toml --intent "..." [--brief-file F]` | Run **one** real attempt with the real runner (fakes elsewhere). Smoke-tests the runner; runs the honeypot demo beat on cue. | ✅ |

## Observer (Dharshan)

| Command | What it does | Status |
|---|---|---|
| `python -m tokeneyezed.observer.service --workspace W --audit-log L --protect P [--port 8765]` | Start the observer service the hook shim talks to. Needs `TOKENEYEZED_OBSERVER_TOKEN`. Proposed alias: `tokeneyezed observe`. | ✅ (alias 📋) |
| `tokeneyezed replay --session B-... --events runs/B/events.jsonl --workspace /absolute/task-repo [--protect /absolute/scorer]` | Replay neutral observer-event JSONL through the deterministic pre-gate and count what it would have caught. Read-only; evaluates pre-tool events only. | 🔨 reader implemented; baseline capture needs Gunjan |
| `tokeneyezed rules learn` | Cluster flags into candidate rules and replay-test them (S1; first thing cut if behind). | 📋 |

## Data (Aaron)

| Command | What it does | Status |
|---|---|---|
| `tokeneyezed db init` | Create collections, the regular indexes (unique `goals.goal_id`, and the rest of the data model), and the Atlas Search / Vector Search indexes (wraps `data/indexes.py:ensure_search_indexes`). Safe to re-run. Search indexes build asynchronously: wait for READY in Atlas. | ✅ (needs `MONGODB_URI`) |
| `tokeneyezed db check [--timeout S]` | The 10:30 embeddings check: embed one test document, insert it into `skills`, get it back from a `$vectorSearch` on `skills_vector`, and delete it. Exits 1 with the reason on failure. Run `db init` first; it refuses (without calling Voyage) if the index isn't READY. | ✅ (needs `MONGODB_URI`, `VOYAGE_API_KEY`) |
| `tokeneyezed db backfill [--limit N]` | Embed attempts written while Voyage was down (wraps `data/writes.py:backfill_embeddings`). | ✅ (needs `MONGODB_URI`, `VOYAGE_API_KEY`) |
| `tokeneyezed eval retrieval SESSION_ID [--arms vector,keyword,hybrid,auto] [--example ATTEMPT_ID] [--auto-interval S]` | Recall@5: vector vs. keyword vs. hybrid, plus the Automated Embedding comparison (wraps `data/retrieval_eval.py`; `--auto-interval 21` on M0). | ✅ (needs `MONGODB_URI`, `VOYAGE_API_KEY`) |

The data commands live in `data/commands.py` and are registered from `controller/cli.py` with `register(subparsers)`, per the convention above. Setup order on a fresh cluster: `db init`, wait for the search indexes to be READY in Atlas, `db check`.

## Task and eval (Gunjan)

| Command | What it does | Status |
|---|---|---|
| `tokeneyezed split` | Build the visible / validation / held-out split from the CommonMark `spec.json`, stratified by section; writes the visible split into the task workspace (`TOKENEYEZED_WORKSPACE`) and, outside it (`TOKENEYEZED_SPLITS_DIR`), the other two plus the harness-side visible copy that `score` reads. | ✅ |
| `tokeneyezed score [--split visible\|validation]` | Score the task workspace; prints the scorer JSON. Backs the `Scorer` port. | ✅ |
| `tokeneyezed baseline --config configs/b.toml` | The naive retry loop (run B): same model, prompt, timebox; no observer, memory, or goals. | 🔨 CLI wired; loop 📋 was due 11:30 |
| `tokeneyezed report [--sessions B-.. H-.. H-mem-..]` | Final numbers and the score chart: held-out pass rate vs. attempt number at equal attempt counts. The only reader of `test_evals`. | 📋 |

## Development (everyone)

| Command | What it does |
|---|---|
| `uv sync` | Install dependencies. |
| `uv run pytest -rs` | All tests; `-rs` lists skipped invariants with owners. |
| `uv run ruff check` / `uv run ruff format` | Lint / format. CI runs both plus pytest. |
| `scripts/sync-check.sh` | What the session-start hook runs: how far behind `main` you are, and any shared-contract diffs. |

## The day's runs

Proven end to end on 2026-09-26 (real Claude, Codex, planner, scorer, observer, Atlas). Each run (B, H, H-mem) gets **its own task workspace**; H and H-mem each get **their own observer** (one observer watches one workspace, on its own port).

**Once:** the split, written harness-side (it also writes the scorer's copy of visible there):

    python -m tokeneyezed.eval.split --hidden-dir ~/tz/splits --visible-dest ~/tz/agent/visible.json

**Per run** (`ws-b`, `ws-h`, `ws-hmem`): `tokeneyezed workspace init ~/tz/ws-h --visible ~/tz/agent/visible.json`

**`.env`** (shared): `MONGODB_URI`, `OPENROUTER_API_KEY`, `VOYAGE_API_KEY`, `TOKENEYEZED_SPLITS_DIR=~/tz/splits`, `TOKENEYEZED_OBSERVER_TOKEN` (any random string). **Per pane**: `TOKENEYEZED_WORKSPACE` (that run's workspace) and, for H and H-mem, `TOKENEYEZED_OBSERVER_URL` (that run's observer).

**Observers** (H and H-mem), each in its own pane, storing events in Atlas:

    python -m tokeneyezed.observer.service --workspace ~/tz/ws-h --mongo --protect ~/tz/splits --port 8765
    python -m tokeneyezed.observer.service --workspace ~/tz/ws-hmem --mongo --protect ~/tz/splits --port 8766

**Runs**, each in its own pane:

    TOKENEYEZED_WORKSPACE=~/tz/ws-b tokeneyezed baseline --config configs/b.toml
    TOKENEYEZED_WORKSPACE=~/tz/ws-h TOKENEYEZED_OBSERVER_URL=http://127.0.0.1:8765/event tokeneyezed run --config configs/h.toml
    TOKENEYEZED_WORKSPACE=~/tz/ws-hmem TOKENEYEZED_OBSERVER_URL=http://127.0.0.1:8766/event tokeneyezed run --config configs/h-mem.toml

**Held-out** (any time, idempotent; per session): `tokeneyezed heldout SESSION --workspace ~/tz/ws-h --heldout ~/tz/splits/heldout.json`

## The live demo, in order

| Beat | Command / screen |
|---|---|
| Score chart | `tokeneyezed report` (or the dashboard) |
| Agent handoff | Ctrl-C in the H pane (`KILLED during attempt #N`), then `tokeneyezed resume H-... --config configs/h.toml --agent <another agent>` (`RESUMED` banner, attempt #N restarts on the new agent) |
| Honeypot | `tokeneyezed attempt --config configs/h.toml --brief-file demo/honeypot.md` (feed shows `BLOCKED pip install markdown-it-py`) |
| Replan | The H pane's `REPLAN` line next to the `goals` document in the Atlas UI |
| Built vs. used | README table |
