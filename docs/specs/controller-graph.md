# Spec: controller graph skeleton + operator interface

**Status:** approved 2026-09-26 · **Owner:** Julian · **Branch:** `controller/graph-skeleton`

## Problem

Nothing runs yet. The controller is the critical path: until it produces real attempts, Aaron's brief builder, Dharshan's post-checks, and Gunjan's `test_evals` have nothing to work on. We also have no agreed way for *us* to drive the harness (start a run, watch it, kill it, resume it on Codex), and no agreed seam between the controller and the other three workstreams' code.

## Approach

### 1. The graph (`controller/graph.py`)

The master plan's flowchart as a LangGraph `StateGraph`, one node per box:

```
START -> seed_goals -> pick_goal -+-> finalize -> END          (no open goal, or budget spent)
                                  +-> build_brief -> plan_attempt -> run_attempt -> score -> review
review -+-> record_flagged -> pick_goal                        (gaming flagged: audit only)
        +-> record_attempt -> check_goal -+-> replan   -> pick_goal   (failure streak >= threshold)
                                          +-> complete -> pick_goal   (val_pass >= goal criteria)
                                          +-> compact  -> pick_goal   (otherwise)
```

**State is small and checkpointed; everything big lives in Atlas.** The state holds `session_id`, `attempt_count`, the active `goal_id`, and the current attempt's `attempt_id`, `intent`, `brief`, and result, plus per-goal failure streaks. Goals, attempts, events, and memory stay in their collections, so the checkpoint never grows with the run.

**Budget counts every attempt, flagged ones included** (open question 3, decided here).

### 2. Ports: the seam to the other workstreams (`src/tokeneyezed/ports.py`)

Nodes never import another workstream's code directly. They call **ports**, `typing.Protocol` interfaces passed in through LangGraph's runtime context (`Runtime[Context]`). The context is *not* checkpointed, which is what makes the Codex handoff free: resume the same checkpoint with a different `AttemptRunner` in the context.

| Port | Implemented by | Methods (draft) |
|---|---|---|
| `GoalStore` | Aaron | `seed(session_id, sections)`, `next_open(session_id)`, `replan(goal_id, note)`, `complete(goal_id)` |
| `BriefBuilder` | Aaron | `build(session_id, goal_id, use_memory) -> str` |
| `Planner` | Julian | `plan(goal, brief) -> intent` |
| `AttemptRunner` | Julian (per agent, see `contracts.md` "Agent adapter") | `run(attempt_id, brief, intent) -> AttemptResult` |
| `Scorer` | Gunjan | `score(workspace) -> {visible_pass, val_pass, per_section}` |
| `Reviewer` | Dharshan | `review(attempt) -> flagged: bool, reasons` |
| `Ledger` | Aaron | `open_attempt(...)` (status `running`), `close_attempt(...)`, `mark_running_as_killed(session_id)` |
| `Compactor` | Aaron | `compact(session_id, goal_id)` |

Each port ships with a **fake** in `controller/fakes.py`, so the whole loop runs end to end today, and each teammate swaps in the real one when it lands. The port signatures become a fifth entry in `docs/contracts.md` at the lock.

### 3. Checkpointing and resume

- `MongoDBSaver` against `MONGODB_URI` for real runs; `InMemorySaver` in tests. `thread_id` = `session_id`.
- **Resume = invoke with `None` on the same thread.** Before resuming, the CLI calls `Ledger.mark_running_as_killed(session_id)` and `AttemptRunner.reset_workspace(last_clean_commit)`, so the in-flight attempt is recorded as killed, never scored, and its half-edits are discarded (open-questions "Codex handoff").
- The heartbeat is deferred: for now, resume is a manual CLI command. A watchdog that auto-resumes stale sessions is a later addition.

### 4. Operator interface: how we drive the harness (`tokeneyezed` CLI)

The master plan says runs start from the CLI and the dashboard is only a supporting view. Proposed commands:

```
tokeneyezed run    --config configs/h.toml [--session-id H-0926]   # new session, runs until done or killed
tokeneyezed resume <session_id> [--agent codex]                    # continue from the latest checkpoint
tokeneyezed status [<session_id>]                                  # goals, attempt count, best val_pass, flags
```

Killing is Ctrl-C / SIGTERM on the `run` process; `resume` then picks it up. This is the live demo beat: kill, `resume --agent codex`, same score.

**Run configs** live in `configs/` as TOML: `base.toml` holds what must be identical across runs (pinned model, attempt budget, per-attempt `max_turns`, failure threshold, workspace and hook paths), and `b.toml`, `h.toml`, `h-mem.toml` override only what differs (`memory = false` for H-mem). One base file is what makes invariant I7 checkable. The baseline and report commands belong to Gunjan (`eval/`) and are out of scope here; they read the same configs.

### 5. Amendment (2026-09-26): the live demo is the interface

The judges see a live demo, not a product pitch, so the interface is whatever reads best on a projector while the real run is already in progress. The H run starts around 1:30 and each attempt takes minutes, so the demo shows a run in flight, not one starting.

- **`tokeneyezed run` prints a live feed** (`controller/live.py`) in a terminal pane that stays open all afternoon: one short block per attempt (number, agent, goal, intent, scores, verdict), printed when the attempt *starts* so a pane is never silent for minutes, plus loud markers for the beats we point at: `FLAGGED`, `REPLAN`, `GOAL COMPLETE`, `KILLED`.
- **Codex handoff beat:** Ctrl-C shows `KILLED during attempt #N`; `tokeneyezed resume <id> --agent codex` shows a `RESUMED` banner with the state restored from Atlas (attempts done, killed attempt id, best score per goal) and restarts attempt #N on Codex. The banner is the proof; nobody has to wait for the attempt to finish.
- **Replan beat:** show the `goals` document in the Atlas UI next to the feed's `REPLAN` line.
- **Score chart and honeypot:** Gunjan's report produces the chart. For the honeypot beat, we should not count on the agent trying `pip install markdown-it-py` on cue during a 3-minute slot: plan a scripted short attempt whose brief asks for it, with the observer's block shown live (needs the runner to surface blocked tool calls; follow-up with Dharshan).

### Out of scope

Real implementations of any port (including the real `claude -p` runner), the heartbeat watchdog, Codex runner, dashboard.

## Tests (the merge gate)

All in `tests/controller/`, using fakes and `InMemorySaver`:

- [x] **Happy path:** with a fake scorer that improves each attempt, every goal completes and the run finalizes.
- [x] **Budget:** the run stops at `max_attempts`, and flagged attempts count toward it.
- [x] **Flagged attempts stay out:** a flagged attempt calls `Ledger.close_attempt` with outcome `flagged` but never reaches `Compactor` or the failure streak.
- [x] **Replan:** N consecutive non-improving attempts on a goal trigger exactly one `GoalStore.replan`.
- [x] **Kill and resume on another agent:** a runner that raises mid-run simulates SIGTERM; resuming the same thread with a *different* fake runner continues from the last completed attempt (no attempt repeated, attempt numbering continues, the new attempts record the new agent).
- [x] **Config:** `h.toml` and `h-mem.toml` resolve to the same model and budget (backs I7).
- [x] **Live feed:** every demo beat appears in order across a kill and a resume on another agent (`tests/controller/test_live.py`).
- [x] `uv run pytest -rs`, `uv run ruff check`, and `uv run ruff format --check` pass.

Ship: PR from this branch, squash-merged after Julian approves.
