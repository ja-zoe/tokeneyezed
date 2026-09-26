---
project: Tokeneyezed
doc: Work split — 4 people
created: 2026-09-26
---

# Tokeneyezed — Work Split (Aaron, Julian, Dharshan, Gunjan)

Goal of this split: **nobody is idle waiting on someone else's output.** The only hard sync point is the **10:45 contract lock** (event format, `attempts` document shape, scorer JSON) — everything before that is designed so each person can build against a *draft* of the piece they need, and everything after it is designed so the piece that unblocks the most people (the attempt runner writing real data) comes online as early as possible.

## Who owns what

| Person | Owns | Why them |
|---|---|---|
| **Aaron** | **Data / MongoDB + Memory** — schema & indexes, events/attempts writes, brief builder, compactor, embeddings, retrieval eval | Already has these schemas drafted from the original SOW (`goals`, `memory`, `skills`) — fastest path to a locked contract that unblocks the other three |
| **Julian** | **Agent Loop** — LangGraph controller, planner, attempt runner, checkpointing/resume, agent handoff | Owns the critical-path piece everyone else's data depends on; can scaffold this with zero dependencies |
| **Dharshan** | **Observer** — event format, shim (Claude Code + Codex), pre-gate, post-checks, gaming review, rule learner | Owns the headless-hook research already in the master plan; runs the 10:30 smoke test |
| **Gunjan** | **Task / Eval / Demo** — CommonMark split + scorer, baseline runner, `test_evals`, dashboard, charts, video, README, submission | Fully decoupled work — can start building and testing without waiting on anyone, and the baseline run only needs the task repo |

## Dependency map (why this order minimizes blocking)

```
Gunjan  ─── scorer JSON ──────────────┐
Dharshan ── event format ─────────────┼──► 10:45 LOCK ──► Julian's attempt runner writes real data
Aaron   ── attempts/goals schema ─────┘                         │
                                                                 ├─► Aaron: brief builder, compactor
                                                                 ├─► Dharshan: post-checks, gaming review
                                                                 └─► Gunjan: dashboard, test_evals
```

Before 10:45, all four are working on pieces that need **no one else's output**:
- Gunjan builds the CommonMark scorer against the public spec (nothing to wait on).
- Dharshan builds the shim + deterministic pre-gate checks against a *draft* event format (pure code, no dependency), then runs the 10:30 smoke test.
- Aaron builds the schema against his own SOW draft, stands up the cluster/indexes, confirms Automated Embedding.
- Julian scaffolds the LangGraph graph + `MongoDBSaver` wiring against a placeholder schema — the graph structure doesn't need the final field names yet.

At 10:45, the three *contract owners* (Aaron, Dharshan, Gunjan) sync for 15 minutes and lock the three shapes. Julian was never blocked because he was building structure, not fields, up to that point.

## Timeline at a glance

| Time | Aaron (Data/Memory) | Julian (Agent Loop) | Dharshan (Observer) | Gunjan (Eval/Demo) |
|---|---|---|---|---|
| 10:30–10:45 | Schema draft + cluster/indexes + embedding check | LangGraph skeleton + `MongoDBSaver` wiring | Draft event format + shim skeleton + **10:30 smoke test** | CommonMark split script + scorer, draft output JSON |
| **10:45** | **Lock: schema** | *(joins lock briefly)* | **Lock: event format** | **Lock: scorer JSON** |
| **11:30** | — | — | — | **Start baseline run B** |
| 11:00–12:45 | Write-path helpers for `events`/`attempts` | Attempt runner (`claude -p` + hooks), writing real attempts | Pre-gate live in the shim; pairs with Julian on wiring | Finishes scorer edge cases; preps `test_evals` writer |
| 12:45–1:30 | Brief builder + compactor + goals/replan | Integration check at lunch | Integration check at lunch | Integration check at lunch |
| **1:30** | — | **Start runs H and H-mem** | — | — |
| 1:30–3:15 | Retrieval eval | Monitors H/H-mem; preps agent handoff | Post-checks, gaming review, rule learner + replay, Codex shim | Dashboard (v0), charts |
| **3:15** | — | **Agent handoff run** (with Dharshan) | Second agent's shim support | — |
| 3:45 | Compute eval numbers | Compute eval numbers | Compute eval numbers | **Freeze runs**, compute eval numbers |
| 4:00–4:45 | Rehearse demo | Rehearse demo | Rehearse demo | Video, README, **submit by 4:45** |

**Cut order if behind, same as the master plan:** skill distillation → rule learner (S1) → H-mem run → agent handoff → dashboard polish → model-based observer checks.

---

## Prompts — copy each block into that person's own coding agent

Each prompt is self-contained (assume the agent has no other context). Paste the whole fenced block, including the "Tokeneyezed" project framing.

### Aaron — Data / MongoDB + Memory

```
I'm working on "Tokeneyezed," a hackathon project for MongoDB's Harness Engineering
& Model Wrangling hackathon (Statement Two: Long Horizon Engineering). A LangGraph
controller runs a headless coding agent (claude -p / codex exec) in a loop against
goals, with an observer gating what reaches memory. Everything is stored in MongoDB
Atlas. I own the DATA / MONGODB + MEMORY workstream. My teammates are Julian (agent
loop / controller), Dharshan (observer), and Gunjan (eval + demo). Nobody else is
blocked on anything I haven't published yet, so my first job is to design and share
these schemas fast.

Build, in order:

1. Stand up an Atlas Sandbox cluster + database. Confirm Automated Embedding works:
   insert one test document into a scratch collection and check that its embedding
   field populates and a vector query returns it. If it doesn't work within ~10
   minutes, fall back to calling the Voyage embeddings API directly (free tier,
   200M tokens) and note that as a blocker resolved.

2. Define and create these collections with indexes:
   - `checkpoints` — managed by LangGraph's MongoDBSaver, no schema of my own.
   - `sessions` — one per run: session_id, status, config (agent, ablation flags),
     created_at, last_checkpoint_at. Index: session_id.
   - `events` — one document PER hook event (never an embedded array inside
     `sessions` — a long run would blow past MongoDB's 16MB document cap):
     session_id, attempt_id, agent, phase (pre/post/stop), tool, input,
     output_summary, verdict, ts. Index: session_id + ts.
   - `goals` — one per spec section per session: status, priority, strategy_notes,
     last_replanned_at, completion_criteria. Index: session_id + status + priority.
   - `attempts` — one per attempt (the ledger): session_id, goal_id, agent, intent,
     diff_summary, commit, visible_pass, val_pass, per_section, outcome,
     observer_flags, parent_attempt, embedding. Vector index on embedding; Atlas
     Search index on intent + diff_summary.
   - `memory` — compacted summaries: summary, embedding, source_event_range.
     Vector index.
   - `skills` — description, embedding, uses, successes, source. Vector index.
   - `interventions` — one per observer flag: event_id, check, reason, evidence,
     embedding. Vector index.
   - `rules` — learned rules: pattern, check_type, evidence_event_ids, replay
     (hits_on_flagged, hits_on_good), status, version.
   - `test_evals` — held-out scores: session_id, attempt_id, test_pass. (Only the
     dashboard and final report read this — the harness itself never touches it.)

3. Write this exact field list up as a short shared doc (or just paste it in our
   team channel) by 10:45 — Dharshan needs the `events` shape, Julian needs
   `attempts`/`goals`, Gunjan needs to know which `attempts` fields the scorer's
   output maps to (visible_pass, val_pass, per_section). This is the most
   important thing I do all morning — everyone else's afternoon depends on it
   landing on time.

4. After the lock, write small helper functions (insert_event, write_attempt,
   upsert_goal, etc.) that Julian's attempt runner and Dharshan's shim can import
   instead of writing raw Mongo calls themselves — reduces the chance of schema
   drift once four people are all touching the same collections under time
   pressure.

5. Once Julian's attempt runner is producing real attempts (expect ~12:45), build:
   - Brief builder: assembles a bounded context for the planner from Atlas —
     best attempt so far, nearest failed attempts on this goal (vector search),
     memory summaries, active rules, relevant skills. Keep the context window
     small and roughly flat in size as the ledger grows.
   - Compactor: summarizes recent attempts into `memory` on goal completion or
     when the ledger for a goal gets long.

6. In the afternoon (~1:30–3:15), once there's enough data, run a retrieval eval:
   recall@5 for "does vector search on `attempts.embedding` surface earlier
   attempts on the same spec section for a new intent," compared against Atlas
   Search keyword search and a hybrid of both. Ground truth is free: attempts on
   the same spec section count as relevant.

Ask me for the Atlas connection string / cluster name before you start creating
collections. Flag immediately if Automated Embedding doesn't work so we can switch
to Voyage without losing time.
```

### Julian — Agent Loop (Controller)

```
I'm working on "Tokeneyezed," a hackathon project for MongoDB's Harness Engineering
& Model Wrangling hackathon (Statement Two: Long Horizon Engineering). A LangGraph
controller runs a headless coding agent (claude -p / codex exec) in a loop against
goals stored in MongoDB Atlas, checkpointed with LangGraph's MongoDBSaver, with an
observer gating memory via hooks. I own the AGENT LOOP workstream. Teammates: Aaron
(data/memory schema), Dharshan (observer/hooks), Gunjan (eval + demo). I am the
most "critical path" piece — Aaron's memory work and Dharshan's post-checks can't
run for real until my attempt runner is producing real attempts — so my priority
is getting a minimal end-to-end loop working fast, then hardening it.

Build, in order (don't wait on anyone for step 1):

1. Scaffold a LangGraph graph with these nodes, wired to LangGraph's
   MongoDBSaver for checkpointing (use a placeholder schema for now — field
   names will get confirmed by Aaron at 10:45, but the graph structure doesn't
   need them yet):
   START (seed one goal per CommonMark spec section) -> pick active goal ->
   check goal open + budget -> brief builder call (stub for now) -> planner
   (states an attempt intent) -> attempt runner -> scorer call (stub) ->
   end-of-attempt review (stub) -> write to ledger -> check failure threshold
   -> replan or continue -> loop, or finish if all goals closed / budget spent.
   Also wire an async heartbeat that can resume from the latest checkpoint in
   Atlas — optionally with a DIFFERENT downstream agent than the one that
   started (this powers the agent-handoff demo beat later).

2. Build the attempt runner: launches a headless coding agent in the task
   repo with the current brief. Two backends:

   Claude Code:
     CLAUDE_CONFIG_DIR=~/.claude-tokeneyezed claude -p "$(cat brief.md)" \
       --settings ~/tokeneyezed/hooks/claude-settings.json \
       --model <pinned-model> --max-turns 40 \
       --permission-mode acceptEdits --allowedTools "Bash(python *),Bash(pytest *)" \
       --permission-prompts none \
       --output-format stream-json --verbose --include-hook-events > attempt.jsonl
     Do NOT use --bare (it skips hook discovery AND refuses subscription login).
     Use a dedicated CLAUDE_CONFIG_DIR so a teammate's personal ~/.claude hooks/
     CLAUDE.md/plugins never contaminate a run. Log in under that config dir once.
     Keep the hooks file OUTSIDE the task repo, passed via --settings, so the
     agent can't edit what it can't see.

   Codex:
     codex exec --dangerously-bypass-hook-trust ...
     (needed because Codex silently skips unreviewed hooks in codex exec mode
     otherwise). Use a dedicated CODEX_HOME the same way as CLAUDE_CONFIG_DIR.

   Coordinate with Dharshan on the exact --settings / hooks.json paths — that's
   the one thing you two need to agree on, ideally right after the 10:45 lock.

3. Write each attempt's result (diff summary, commit, intent, per-section pass
   rates once the scorer exists, observer_flags) into `attempts` using Aaron's
   schema and his write-helper once it exists.

4. Pin the same model across all agent invocations for the B / H / H-mem
   comparison — read the model string from one shared config, don't hardcode
   it per-run.

5. At 1:30, kick off runs H (full harness) and H-mem (harness with memory/brief
   retrieval disabled via a config flag — same brief builder call but skip the
   ledger-retrieval and memory-summary parts).

6. At 3:15, do the agent handoff demo run: kill the H run (SIGTERM — this exits
   143, drops the in-progress turn, runs only SessionEnd hooks; fine, since our
   state lives in Atlas, not in the agent's own session), then resume from the
   latest checkpoint using codex exec instead of claude -p. Confirm the score
   doesn't regress.

Ask me for: the model to pin, and Aaron's finalized attempts/goals field names
once he shares them (~10:45). Don't block on the observer/scorer being fully
built to test your own plumbing — stub their calls first, swap in the real
ones as Dharshan and Gunjan finish.
```

### Dharshan — Observer

```
I'm working on "Tokeneyezed," a hackathon project for MongoDB's Harness Engineering
& Model Wrangling hackathon (Statement Two: Long Horizon Engineering). A LangGraph
controller runs a headless coding agent (claude -p / codex exec); I own the
OBSERVER workstream — the hooks-based gate that decides what tool calls are
allowed and what attempts are allowed into memory. Teammates: Aaron (data/memory
schema), Julian (agent loop / attempt runner), Gunjan (eval + demo). Nothing about
my early work depends on them, so I start immediately.

Build, in order:

1. Draft a neutral event format (one shape regardless of whether the source is
   Claude Code or Codex hooks): {session_id, attempt_id, agent, phase (pre/post/
   stop), tool, input, output_summary, verdict, ts}. Share this with Aaron
   (feeds his `events` collection) and Julian (feeds his hook config) at the
   10:45 lock.

2. Build a small shim service: one HTTP endpoint hooks call, which converts
   whatever the agent's native hook payload looks like into the neutral format,
   posts it to the observer, and converts the observer's decision back into
   whatever that agent expects. Build this against Claude Code's hook format
   first, add a Codex adapter later — the observer logic itself should be
   agent-agnostic.

3. Build the deterministic PRE-gate checks (these run before a tool executes,
   so blocking here actually prevents the action — don't defer these to after
   the tool runs):
   - Honeypot / forbidden shortcut: pattern-match for `pip install
     markdown-it-py`, `mistune`, `commonmark`, or copying from site-packages.
   - Tampering: path-match edits to the scorer, test files, or hook configs
     (including our own .claude/ or .codex/ directories).
   - Destructive actions: plain code first (rm -rf, git reset --hard, etc.);
     only fall back to a small model call if the pattern is ambiguous.
   - Learned rules: hot-reload from the `rules` collection via a MongoDB
     change stream (this collection won't have real rules until the afternoon,
     but wire the change-stream listener now).
   Deterministic checks fail CLOSED (block on error/uncertainty). Model-based
   checks fail OPEN (a model outage shouldn't halt the run).
   Output convention: exit 0 with no stdout to allow (works for both agents);
   for Claude Code hooks use exit code 2 + stderr to block with a reason.

4. At 10:30, run the headless-hook smoke test (10 minutes) — do this yourself,
   don't delegate: for both claude -p and codex exec, run a headless session
   with a hook that (a) appends every event it receives to a log file and
   (b) denies any command containing TOKENEYEZED_HONEYPOT. Prompt the agent to
   run `echo TOKENEYEZED_HONEYPOT` and then edit a file. Check:
   - Hooks fire at all (log has pre + post events for both the command and the
     edit — if empty, check CLAUDE_CONFIG_DIR / CODEX_HOME and Codex's
     --dangerously-bypass-hook-trust flag).
   - Deny actually works (the echoed string never appears in the transcript).
   - Edit coverage is logged, not just Bash (matters most for Codex — some
     docs claim PreToolUse is Bash-only, others say it covers edits + MCP too;
     the installed version decides, so this test is the real answer).
   - No personal hooks/CLAUDE.md/plugins leak into the run (check the
     system/init event).
   Test the logging hook by hand with sample JSON BEFORE trusting an empty
   log — "no events" might just mean the logger itself is broken.

5. After 11:00, pair with Julian to wire the shim into his real attempt
   runner as it comes online.

6. In the afternoon (1:30–3:15), in priority order (cut from the bottom if
   behind):
   - POST-checks: declared-intent match (files/sections touched vs. the
     planner's stated intent), repeat-failure (vector similarity to earlier
     FAILED attempts on this goal — needs Aaron's retrieval helpers), no-
     progress (N tool calls with no file change or test run). All produce a
     corrective note back to the agent, not a hard block.
   - End-of-attempt gaming review: visible pass rate rising while validation
     stays flat, or code that special-cases test inputs → the attempt is kept
     OUT of memory/metrics entirely, logged for audit.
   - Rule learner (S1, cut first if time-starved): cluster repeated flags,
     propose a candidate rule, replay it over stored `interventions`/`events`
     to prove it actually distinguishes flagged from good steps before
     promoting it to `rules` (never mid-run).
   - Second agent's shim support for Julian's 3:15 agent handoff.

Ask me for the exact --settings / hooks.json path convention Julian's attempt
runner will use, right after 10:45.
```

### Gunjan — Task / Eval / Demo

```
I'm working on "Tokeneyezed," a hackathon project for MongoDB's Harness
Engineering & Model Wrangling hackathon (Statement Two: Long Horizon
Engineering). A LangGraph controller runs a headless coding agent against a
task; a MongoDB-backed observer and memory make it improve faster than a naive
retry loop. I own TASK / EVAL / DEMO. Teammates: Aaron (data/memory), Julian
(agent loop), Dharshan (observer). It is 12:46 PM; the baseline run was due at
11:30, so run B is the top priority — everything else serves getting it started.

You are working in the tokeneyezed repo (github.com/ja-zoe/tokeneyezed) on
branch `eval/pipeline`. Repo rules that bind you:
- Python 3.12+, uv, ruff line-length 100 (select E,F,I,UP,B). Run
  `uv run ruff check` before committing.
- Read AGENTS.md first and obey its sync-at-task-boundary rule.
- ALL my code lives in `src/tokeneyezed/eval/` — invariant I3 says the string
  "test_evals" may appear in no source file outside that package. Do not touch
  controller/, data/, or observer/ except the one-line CLI registration below.
- Tests use `tests/mongo_fakes.py` fakes — no live Mongo in unit tests.
- Per tests/INVARIANTS.md: a check that can't run yet is *skipped with the
  owner's name*, never passed, and every real check must prove it detects a
  planted violation.

STEP 0 — Port what's already built. Copy from ~/MongoDB_Planner/eval/ into the
repo (these are finished and end-to-end verified; adapt style, don't rewrite
logic):
- data/spec-0.31.2.json  ->  src/tokeneyezed/eval/data/spec-0.31.2.json
  (652 CommonMark examples, 26 sections; sections match configs/base.toml).
- split_spec.py  ->  src/tokeneyezed/eval/split.py. Deterministic stratified
  split, seed 20260926: visible 196 / validation 234 / heldout 222. Tiny-section
  priority validation->heldout->visible (Precedence, Blank lines, Inlines are
  val-only; Soft line breaks is val+heldout), so per_section.<s>.visible can be
  null. It refuses to write the hidden dir under the visible destination (I1
  guard). Writes manifest.json with the spec sha256 + per-split example ids.
- scorer.py  ->  src/tokeneyezed/eval/scorer.py. Modes attempt/heldout/single.
  Renderer contract: run `--program` (default `python3 render.py`) with
  cwd = task workspace, Markdown UTF-8 on stdin, HTML on stdout; exact match
  with the spec's html, trailing newline tolerated; 5 s/example timeout;
  crashes count as errors, never passes. ~3.5 s per attempt at --jobs 8.
- The locked output contract is ~/MongoDB_Planner/eval/scorer-output.draft.json:
  attempt mode emits top-level visible_pass, val_pass,
  per_section = {"<section>": {"visible": x|null, "val": y}}, plus
  scorer_version, spec_version, counts, duration_s — these merge verbatim into
  Aaron's `attempts` doc. heldout mode emits test_pass ONLY, which goes ONLY to
  the `test_evals` collection. Do not rename any field without asking me.

STEP 1 — CLI registration (docs/commands.md convention). Create
src/tokeneyezed/eval/cli.py exposing register(subparsers), and add the one call
in controller/cli.py that wires it in. Subcommands, exactly as commands.md
plans them:
- `tokeneyezed split`     — build the three splits; visible file goes INTO the
  task workspace at tests/visible.json, hidden dir stays OUTSIDE it.
- `tokeneyezed score [--split visible|validation]` — score the workspace,
  print the scorer JSON (backs Julian's Scorer port).
- `tokeneyezed baseline --config configs/b.toml` — run B (step 3).
- `tokeneyezed report [--sessions ...]` — final numbers + score chart (step 5).
Machine-specific paths come from .env (see .env.example); shared values from
configs/base.toml (max_attempts=30, max_turns=40, target_val_pass=0.85 — never
hardcode these).

STEP 2 — The task workspace. Create src/tokeneyezed/eval/task_template/ with:
- render.py stub: reads Markdown from stdin, writes HTML to stdout; body is
  `sys.stdout.write(sys.stdin.read())` so attempt 0 scores near zero honestly.
- tests/visible.json — placed by `tokeneyezed split`.
- run_tests.py — lets the agent score itself on visible examples only.
- PROMPT.md — the task prompt given to the agent under test. It must say,
  verbatim-stable across B, H, and H-mem (the runs are only comparable if the
  prompt is identical):
    "Build a CommonMark (Markdown -> HTML) renderer from scratch in pure
    Python. Your repo must contain render.py at the root that reads Markdown
    (UTF-8) on stdin and writes HTML on stdout. You may not install or import
    any existing Markdown library (markdown, markdown-it-py, mistune,
    commonmark, marko, mistletoe...) — implement the algorithm yourself.
    python3 run_tests.py runs the visible spec examples in tests/visible.json;
    make as many pass as you can. Standard library only."
  The "no markdown libraries" clause is the honeypot Dharshan's observer
  blocks — keep the library names in the prompt matching his blocklist.
Add a workspace-setup function Julian's runner can call: copy template to a
fresh dir, git init, place visible.json. The hidden splits dir must not be
reachable from any path under the workspace (invariant I1).

STEP 3 — Baseline run B (LATE — was due 11:30; get this running before
polishing anything). A naive retry loop, no LangGraph, no observer, no memory,
no goals: for attempt in 1..max_attempts: run the agent on PROMPT.md plus, from
attempt 2 on, one line: "Previous attempt passed {visible_pass:.0%} of visible
tests. Improve render.py."; then score with scorer attempt mode; log one
attempts-shaped JSON per attempt to runs/<session_id>/attempts.jsonl AND, when
MONGODB_URI is set, insert into Aaron's `attempts` collection with agent="B".
Mirror the approved runner invocation from docs/specs/claude-runner.md — same
pinned model, --max-turns from base.toml, --permission-mode acceptEdits,
--output-format stream-json, dedicated CLAUDE_CONFIG_DIR, per-attempt wall-clock
timebox with SIGTERM (a timed-out attempt still counts and is scored as-is) —
but WITHOUT hook settings or observer env vars: B being observer-blind is the
point of the comparison. Comparisons are at equal attempt counts, never
wall-clock. The model string is "" in configs/base.toml until pinned — ASK ME
before the first real attempt; do not invent one.

STEP 4 — test_evals writer + invariants. In src/tokeneyezed/eval/ only:
write_test_eval(session_id, attempt_id, test_pass) inserting into `test_evals`
(exactly those three fields), fed by scorer heldout mode. The harness NEVER
reads this collection; only `tokeneyezed report` and the dashboard do. Then
replace the I1 and I3 pytest skips in tests/test_invariants.py with real
checks: I1 walks the workspace proving no path reaches the hidden split dir;
I3 greps src/ proving "test_evals" appears only under src/tokeneyezed/eval/.
Prove each detects a planted violation (plant it in tmp, assert detection,
clean up).

STEP 5 — `tokeneyezed report`: read `attempts` (and test_evals) for the named
sessions, print held-out pass rate vs attempt number for B / H / H-mem capped
at equal attempt counts, and write a matplotlib PNG of the score chart for the
README and video. One run per configuration is a demonstration, not
statistical significance — the report should print that caveat so we say it
before a judge asks.

Testing: end-to-end only, using the repo's fakes for Mongo and a fake `claude`
executable on PATH for the baseline loop (the claude-runner spec's test does
the same). Verify the ported scorer still scores markdown-it-py ≈0.99/1.0/0.99
and a crashing renderer as errors. Run `uv run ruff check` and the test suite;
open a PR titled "eval: split, scorer, task template, baseline B, test_evals".

Ask me for: (1) the pinned model string, (2) Aaron's final `attempts` field
names and whether nulls in per_section.<s>.visible are accepted, (3) whether
counts/duration_s ride along into `attempts`. Do not guess any of the three.
```
