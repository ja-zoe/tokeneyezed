# Command interface

Every command we should be able to run, who owns it, and whether it exists yet. One entry point, `tokeneyezed`, with a subcommand per job, so the demo and the day's runs never depend on remembering `python -m` paths.

**Status key:** ✅ exists on `main` · 🔨 in progress · 📋 planned (owner builds it) · ⚠️ blocking something

**Convention for adding a subcommand:** each workstream keeps its commands in its own package (e.g. `eval/cli.py`) and exposes `register(subparsers)`; `controller/cli.py` calls it. That keeps four people out of one file. Machine-specific paths come from `.env` (see `.env.example`); shared behavior comes from `configs/`.

## Runs (controller, Julian)

| Command | What it does | Status |
|---|---|---|
| `tokeneyezed run --config configs/h.toml [--session-id ID]` | Start a session and stream the live feed until done or killed (Ctrl-C). Configs: `b.toml`, `h.toml`, `h-mem.toml`. | ✅ with `--fake`; real ports 📋 |
| `tokeneyezed run ... --fake [--checkpointer memory]` | The whole loop on in-memory fakes, for trying things without Atlas or an agent. | ✅ |
| `tokeneyezed resume ID --config configs/h.toml [--agent AGENT]` | Continue a session from its Atlas checkpoint: marks the killed attempt, resets the workspace, prints the `RESUMED` banner, and carries on, optionally on a different agent (the agent handoff). | ✅ code; needs real ports 📋 |
| `tokeneyezed status ID` | Session progress from its checkpoint: attempts, current goal, best score per goal. | ✅ (needs `MONGODB_URI`) |
| `tokeneyezed attempt --config configs/h.toml --intent "..." [--brief-file F]` | Run **one** real attempt with the real runner (fakes elsewhere). Smoke-tests the runner; runs the honeypot demo beat on cue. | ✅ |

## Observer (Dharshan)

| Command | What it does | Status |
|---|---|---|
| `python -m tokeneyezed.observer.service --workspace W --audit-log L --protect P [--port 8765]` | Start the observer service the hook shim talks to. Needs `TOKENEYEZED_OBSERVER_TOKEN`. Proposed alias: `tokeneyezed observe`. | ✅ (alias 📋) |
| `tokeneyezed replay --session B-...` | Run a recorded session's events through the observer and count what it would have caught (the observer eval). | 📋 |
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
| `tokeneyezed split` | Build the visible / validation / held-out split from the CommonMark `spec.json`, stratified by section; writes the visible split into the task workspace and the other two outside it. | ⚠️ 📋 nothing on GitHub yet |
| `tokeneyezed score [--split visible\|validation]` | Score the task workspace; prints the scorer JSON. Backs the `Scorer` port. | ⚠️ 📋 |
| `tokeneyezed baseline --config configs/b.toml` | The naive retry loop (run B): same model, prompt, timebox; no observer, memory, or goals. | ⚠️ 📋 was due 11:30 |
| `tokeneyezed report [--sessions B-.. H-.. H-mem-..]` | Final numbers and the score chart: held-out pass rate vs. attempt number at equal attempt counts. The only reader of `test_evals`. | 📋 |

## Development (everyone)

| Command | What it does |
|---|---|
| `uv sync` | Install dependencies. |
| `uv run pytest -rs` | All tests; `-rs` lists skipped invariants with owners. |
| `uv run ruff check` / `uv run ruff format` | Lint / format. CI runs both plus pytest. |
| `scripts/sync-check.sh` | What the session-start hook runs: how far behind `main` you are, and any shared-contract diffs. |

## The day's runs

1. `python -m tokeneyezed.observer.service ...` in its own pane (stays up all day).
2. `tokeneyezed baseline --config configs/b.toml` (as soon as the scorer exists).
3. `tokeneyezed run --config configs/h.toml` and `tokeneyezed run --config configs/h-mem.toml`, each in its own pane (target 1:30).

## The live demo, in order

| Beat | Command / screen |
|---|---|
| Score chart | `tokeneyezed report` (or the dashboard) |
| Agent handoff | Ctrl-C in the H pane (`KILLED during attempt #N`), then `tokeneyezed resume H-... --config configs/h.toml --agent <another agent>` (`RESUMED` banner, attempt #N restarts on the new agent) |
| Honeypot | `tokeneyezed attempt --config configs/h.toml --brief-file demo/honeypot.md` (feed shows `BLOCKED pip install markdown-it-py`) |
| Replan | The H pane's `REPLAN` line next to the `goals` document in the Atlas UI |
| Built vs. used | README table |
